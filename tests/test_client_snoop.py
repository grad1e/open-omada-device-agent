import ipaddress
import struct

import pytest

from open_omada_device_agent.adapters.outbound.openwrt.client_observations import (
    merge_associated_wireless_client_states,
)
from open_omada_device_agent.adapters.outbound.openwrt.client_snoop import (
    SNOOP_FILTER,
    BridgeClientSnooper,
    ClientAddressTable,
    parse_frame,
)
from open_omada_device_agent.contexts.clients.domain import WirelessClientState

CLIENT = bytes.fromhex("02aabbccdd01")
ROUTER = bytes.fromhex("02aabbccdd99")
BROADCAST = b"\xff" * 6


def _eth(src, ether_type, payload, dst=BROADCAST):
    return dst + src + struct.pack("!H", ether_type) + payload


def _ipv4_udp(src_ip, dst_ip, sport, dport, body, *, frag=0):
    udp = struct.pack("!HHHH", sport, dport, 8 + len(body), 0) + body
    header = struct.pack(
        "!BBHHHBBH4s4s", 0x45, 0, 20 + len(udp), 0, frag, 64, 17, 0,
        ipaddress.IPv4Address(src_ip).packed, ipaddress.IPv4Address(dst_ip).packed,
    )
    return header + udp


def _ipv6(src_ip, next_header, body, dst_ip="ff02::1"):
    header = struct.pack("!IHBB", 6 << 28, len(body), next_header, 255)
    return header + ipaddress.IPv6Address(src_ip).packed + ipaddress.IPv6Address(dst_ip).packed + body


def _dhcp(op, message_type, *, chaddr=CLIENT, yiaddr="0.0.0.0", options=()):
    fixed = struct.pack("!BBBBIHH4s4s4s4s", op, 1, 6, 0, 1, 0, 0x8000, b"\0" * 4,
                        ipaddress.IPv4Address(yiaddr).packed, b"\0" * 4, b"\0" * 4)
    fixed += chaddr + b"\0" * 10 + b"\0" * 192 + b"\x63\x82\x53\x63"
    opts = bytes([53, 1, message_type])
    for code, value in options:
        opts += bytes([code, len(value)]) + value
    return fixed + opts + b"\xff"


def _dhcp_request(hostname=b"Pad-6", requested="192.168.0.101"):
    body = _dhcp(1, 3, options=((50, ipaddress.IPv4Address(requested).packed), (12, hostname)))
    return _eth(CLIENT, 0x0800, _ipv4_udp("0.0.0.0", "255.255.255.255", 68, 67, body))


def _arp(sender_ip, sender_mac=CLIENT):
    payload = struct.pack("!HHBBH", 1, 0x0800, 6, 4, 1) + sender_mac
    payload += ipaddress.IPv4Address(sender_ip).packed + b"\0" * 6 + ipaddress.IPv4Address("192.168.0.1").packed
    return _eth(sender_mac, 0x0806, payload)


def _mdns_response(name, address):
    def encode(text):
        return b"".join(bytes([len(part)]) + part.encode() for part in text.split(".")) + b"\0"

    header = struct.pack("!HHHHHH", 0, 0x8400, 0, 1, 0, 0)
    ip = ipaddress.ip_address(address)
    rtype = 1 if ip.version == 4 else 28
    record = encode(name) + struct.pack("!HHIH", rtype, 0x8001, 120, len(ip.packed)) + ip.packed
    return header + record


# --- parser --------------------------------------------------------------------


def test_dhcp_request_gives_name_and_requested_address():
    (obs,) = parse_frame(_dhcp_request())

    assert (obs.mac, obs.ipv4, obs.hostname) == ("02:aa:bb:cc:dd:01", "192.168.0.101", "Pad-6")


def test_dhcp_ack_gives_assigned_address_for_chaddr():
    body = _dhcp(2, 5, yiaddr="192.168.0.120")
    frame = _eth(ROUTER, 0x0800, _ipv4_udp("192.168.0.1", "255.255.255.255", 67, 68, body))

    (obs,) = parse_frame(frame)

    assert (obs.mac, obs.ipv4, obs.hostname) == ("02:aa:bb:cc:dd:01", "192.168.0.120", None)


def test_dhcp_discover_without_name_is_ignored():
    body = _dhcp(1, 1)
    frame = _eth(CLIENT, 0x0800, _ipv4_udp("0.0.0.0", "255.255.255.255", 68, 67, body))

    assert parse_frame(frame) == ()


def test_arp_gives_sender_address_and_skips_probes():
    (obs,) = parse_frame(_arp("192.168.0.104"))

    assert (obs.mac, obs.ipv4) == ("02:aa:bb:cc:dd:01", "192.168.0.104")
    assert parse_frame(_arp("0.0.0.0")) == ()


def test_neighbour_discovery_gives_ipv6_but_dad_probes_do_not():
    ns = bytes([135, 0, 0, 0]) + b"\0" * 20
    (obs,) = parse_frame(_eth(CLIENT, 0x86DD, _ipv6("fe80::1036:6b0b:53ce:e8c", 58, ns)))

    assert obs.ipv6 == ("fe80::1036:6b0b:53ce:e8c",)
    assert parse_frame(_eth(CLIENT, 0x86DD, _ipv6("::", 58, ns))) == ()


def test_mdns_name_only_for_the_senders_own_address():
    own = _mdns_response("Living-Room-iPad.local", "192.168.0.50")
    frame = _eth(CLIENT, 0x0800, _ipv4_udp("192.168.0.50", "224.0.0.251", 5353, 5353, own))
    (obs,) = parse_frame(frame)
    assert (obs.ipv4, obs.hostname) == ("192.168.0.50", "Living-Room-iPad")

    # A record for someone else's address gives no name, only the sender's own address.
    other = _mdns_response("Printer.local", "192.168.0.77")
    frame = _eth(CLIENT, 0x0800, _ipv4_udp("192.168.0.50", "224.0.0.251", 5353, 5353, other))
    (obs,) = parse_frame(frame)
    assert (obs.ipv4, obs.hostname) == ("192.168.0.50", None)


def test_mdns_query_gives_the_senders_own_addresses():
    query = struct.pack("!HHHHHH", 0, 0, 1, 0, 0, 0) + b"\x05_http\x04_tcp\x05local\x00" + b"\x00\x0c\x00\x01"
    frame = _eth(CLIENT, 0x0800, _ipv4_udp("192.168.0.50", "224.0.0.251", 5353, 5353, query))
    (obs,) = parse_frame(frame)
    assert (obs.mac, obs.ipv4, obs.hostname) == ("02:aa:bb:cc:dd:01", "192.168.0.50", None)

    udp = struct.pack("!HHHH", 5353, 5353, 8 + len(query), 0) + query
    (obs,) = parse_frame(_eth(CLIENT, 0x86DD, _ipv6("fe80::7", 17, udp, dst_ip="ff02::fb")))
    assert obs.ipv6 == ("fe80::7",)


def test_mdns_over_ipv6():
    body = _mdns_response("tv.local", "fe80::5")
    udp = struct.pack("!HHHH", 5353, 5353, 8 + len(body), 0) + body
    (obs,) = parse_frame(_eth(CLIENT, 0x86DD, _ipv6("fe80::5", 17, udp, dst_ip="ff02::fb")))

    assert (obs.ipv6, obs.hostname, obs.ipv4) == (("fe80::5",), "tv", None)


@pytest.mark.parametrize(
    "frame",
    [
        b"\x00" * 10,
        _eth(CLIENT, 0x88CC, b"\0" * 40),
        _eth(CLIENT, 0x0800, _ipv4_udp("192.168.0.5", "8.8.8.8", 5000, 53, b"\0" * 20)),
        _eth(CLIENT, 0x0800, _ipv4_udp("0.0.0.0", "255.255.255.255", 68, 67, b"\0" * 50)),
        _eth(CLIENT, 0x0800, _ipv4_udp("0.0.0.0", "224.0.0.251", 5353, 5353, b"\x00" * 12)),
    ],
)
def test_unrelated_or_broken_frames_are_ignored(frame):
    assert parse_frame(frame) == ()


# --- table ---------------------------------------------------------------------


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_table_prefers_dhcp_over_arp_until_the_dhcp_value_is_old():
    clock = _Clock()
    table = ClientAddressTable(clock=clock)
    table.observe(parse_frame(_dhcp_request(requested="192.168.0.101")))
    table.observe(parse_frame(_arp("192.168.0.222")))

    (client,) = table.clients()
    assert (client.ipv4, client.hostname) == ("192.168.0.101", "Pad-6")

    clock.now += 301
    table.observe(parse_frame(_arp("192.168.0.222")))
    assert table.clients()[0].ipv4 == "192.168.0.222"


def test_table_ages_out_addresses_before_names():
    clock = _Clock()
    table = ClientAddressTable(clock=clock, address_ttl=60, name_ttl=600)
    table.observe(parse_frame(_dhcp_request()))

    clock.now += 61
    (client,) = table.clients()
    assert (client.ipv4, client.hostname) == (None, "Pad-6")

    clock.now += 600
    assert table.clients() == ()


def test_table_is_bounded():
    clock = _Clock()
    table = ClientAddressTable(clock=clock, max_clients=2)
    for index in range(3):
        clock.now += 1
        mac = bytes.fromhex(f"02aabbccdd0{index}")
        table.observe(parse_frame(_arp(f"192.168.0.{10 + index}", sender_mac=mac)))

    assert [client.mac for client in table.clients()] == ["02:aa:bb:cc:dd:01", "02:aa:bb:cc:dd:02"]


def test_learned_details_only_enrich_associated_clients():
    table = ClientAddressTable()
    table.observe(parse_frame(_dhcp_request()))
    table.observe(parse_frame(_arp("192.168.0.9", sender_mac=ROUTER)))
    associated = (WirelessClientState(mac="02:aa:bb:cc:dd:01", rx_bytes=5),)

    (client,) = merge_associated_wireless_client_states(associated, table.clients())

    assert (client.ipv4, client.hostname, client.rx_bytes) == ("192.168.0.101", "Pad-6", 5)


def test_snooper_off_never_opens_sockets():
    discovered = []
    snooper = BridgeClientSnooper("off", discover=lambda: discovered.append(1) or ())

    assert snooper.clients() == ()
    assert discovered == []


# --- kernel filter -------------------------------------------------------------


def _run_bpf(program, packet):
    """Interpret the subset of classic BPF the snoop filter uses."""
    a = x = 0
    pc = 0
    while True:
        code, jt, jf, k = program[pc]
        pc += 1
        if code == 0x28:
            a = struct.unpack_from("!H", packet, k)[0] if k + 2 <= len(packet) else None
        elif code == 0x30:
            a = packet[k] if k < len(packet) else None
        elif code == 0x48:
            a = struct.unpack_from("!H", packet, x + k)[0] if x + k + 2 <= len(packet) else None
        elif code == 0xB1:
            x = (packet[k] & 0x0F) * 4
        elif code == 0x06:
            return k
        else:
            if a is None:
                return 0  # an out-of-bounds load ends the program with "reject"
            test = {0x15: a == k, 0x35: a >= k, 0x25: a > k, 0x45: bool(a & k)}[code]
            pc += jt if test else jf
        if a is None:
            return 0


def _filter_accepts(frame):
    return _run_bpf(SNOOP_FILTER, frame) > 0


def test_filter_passes_only_the_packets_the_parser_uses():
    ns = bytes([135, 0, 0, 0]) + b"\0" * 20
    echo = bytes([128, 0, 0, 0]) + b"\0" * 4
    mdns6 = struct.pack("!HHHH", 5353, 5353, 20, 0) + b"\0" * 12
    accepted = [
        _dhcp_request(),
        _arp("192.168.0.104"),
        _eth(CLIENT, 0x86DD, _ipv6("fe80::1", 58, ns)),
        _eth(CLIENT, 0x86DD, _ipv6("fe80::1", 17, mdns6, dst_ip="ff02::fb")),
        _eth(CLIENT, 0x0800, _ipv4_udp("192.168.0.5", "224.0.0.251", 5353, 5353, b"\0" * 12)),
    ]
    rejected = [
        _eth(CLIENT, 0x0800, _ipv4_udp("192.168.0.5", "8.8.8.8", 5000, 53, b"\0" * 20)),
        _eth(CLIENT, 0x0800, _ipv4_udp("192.168.0.5", "1.1.1.1", 68, 67, b"\0", frag=5)),
        _eth(CLIENT, 0x86DD, _ipv6("fe80::1", 58, echo)),
        _eth(CLIENT, 0x86DD, _ipv6("fe80::1", 6, b"\0" * 20)),
        _eth(CLIENT, 0x88CC, b"\0" * 40),
    ]

    assert [_filter_accepts(frame) for frame in accepted] == [True] * len(accepted)
    assert [_filter_accepts(frame) for frame in rejected] == [False] * len(rejected)
