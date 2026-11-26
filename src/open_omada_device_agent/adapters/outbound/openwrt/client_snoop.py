"""Learn Wi-Fi client addresses and names by listening on the AP's bridges.

A bridge-only AP hands out no DHCP leases and rarely talks IP to its clients,
so its own lease file and neighbour table stay empty. The broadcast and
multicast traffic that crosses the bridge still says who is who:

- DHCP DISCOVER/REQUEST carry the client's host name (option 12) and the
  address it wants (option 50); a broadcast ACK carries the assigned address.
- ARP requests carry the sender's IPv4 address.
- IPv6 neighbour discovery carries the sender's IPv6 addresses.
- mDNS is link-local multicast, so every mDNS packet's source address is the
  sender's own; responses and probes also announce ``<name>.local``.

The listener only receives. A kernel BPF filter passes just those packets, so
user traffic never reaches Python.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import ipaddress
import logging
import os
from pathlib import Path
import select
import socket
import struct
import threading
import time
from typing import Callable, Iterable

from ....contexts.clients.domain import WirelessClientState

log = logging.getLogger("open_omada.client_snoop")

ETH_P_ALL = 0x0003
SO_ATTACH_FILTER = 26
ETH_HEADER = 14

ADDRESS_TTL = 3600.0
NAME_TTL = 86400.0
MAX_CLIENTS = 1024
MAX_IPV6 = 4
MAX_NAME = 64

# Higher ranks are better evidence. A lower rank may still replace a stale value.
RANK_ARP = 1
RANK_MDNS = 2
RANK_DHCP = 3
REPLACE_AFTER = 300.0


@dataclass(frozen=True)
class Observation:
    mac: str
    ipv4: str | None = None
    ipv4_rank: int = 0
    ipv6: tuple[str, ...] = ()
    hostname: str | None = None
    hostname_rank: int = 0


# --- frame parsing ---------------------------------------------------------


def parse_frame(frame: bytes) -> tuple[Observation, ...]:
    """Return what one Ethernet frame says about its sender, if anything."""
    if len(frame) < ETH_HEADER:
        return ()
    src_mac = frame[6:12]
    ether_type = struct.unpack_from("!H", frame, 12)[0]
    payload = frame[ETH_HEADER:]
    if ether_type == 0x0806:
        return _parse_arp(payload)
    if ether_type == 0x0800:
        return _parse_ipv4(src_mac, payload)
    if ether_type == 0x86DD:
        return _parse_ipv6(src_mac, payload)
    return ()


def _parse_arp(payload: bytes) -> tuple[Observation, ...]:
    if len(payload) < 28:
        return ()
    htype, ptype, hlen, plen = struct.unpack_from("!HHBB", payload, 0)
    if (htype, ptype, hlen, plen) != (1, 0x0800, 6, 4):
        return ()
    sender = _usable_ipv4(payload[14:18])
    mac = _mac(payload[8:14])
    if sender is None or mac is None:
        return ()
    return (Observation(mac=mac, ipv4=sender, ipv4_rank=RANK_ARP),)


def _parse_ipv4(src_mac: bytes, payload: bytes) -> tuple[Observation, ...]:
    if len(payload) < 20 or payload[0] >> 4 != 4 or payload[9] != 17:
        return ()
    if struct.unpack_from("!H", payload, 6)[0] & 0x1FFF:
        return ()
    ihl = (payload[0] & 0x0F) * 4
    udp = payload[ihl:]
    if len(udp) < 8:
        return ()
    src_port, dst_port = struct.unpack_from("!HH", udp, 0)
    body = udp[8:]
    if {src_port, dst_port} <= {67, 68}:
        return _parse_dhcp(body)
    if dst_port == 5353 and src_port == 5353:
        source = _usable_ipv4(payload[12:16])
        mac = _mac(src_mac)
        if source is None or mac is None:
            return ()
        return _mdns_observation(mac, body, source, ipv6=False)
    return ()


def _parse_ipv6(src_mac: bytes, payload: bytes) -> tuple[Observation, ...]:
    if len(payload) < 40 or payload[0] >> 4 != 6:
        return ()
    mac = _mac(src_mac)
    source = _usable_ipv6(payload[8:24])
    if mac is None or source is None:
        return ()
    next_header = payload[6]
    body = payload[40:]
    if next_header == 58 and body and 133 <= body[0] <= 136:
        return (Observation(mac=mac, ipv6=(source,)),)
    if next_header == 17 and len(body) >= 8 and struct.unpack_from("!HH", body, 0) == (5353, 5353):
        return _mdns_observation(mac, body[8:], source, ipv6=True)
    return ()


def _mdns_observation(mac: str, message: bytes, source: str, *, ipv6: bool) -> tuple[Observation, ...]:
    named = _parse_mdns(mac, message, own_address=source)
    if named:
        return named
    if ipv6:
        return (Observation(mac=mac, ipv6=(source,)),)
    return (Observation(mac=mac, ipv4=source, ipv4_rank=RANK_MDNS),)


def _parse_dhcp(body: bytes) -> tuple[Observation, ...]:
    if len(body) < 240 or body[236:240] != b"\x63\x82\x53\x63":
        return ()
    op, htype, hlen = body[0], body[1], body[2]
    if htype != 1 or hlen != 6:
        return ()
    mac = _mac(body[28:34])
    if mac is None:
        return ()
    options = _dhcp_options(body[240:])
    message_type = options.get(53, b"\x00")[0]
    ipv4 = None
    if op == 1 and message_type == 3:  # REQUEST
        ipv4 = _usable_ipv4(options.get(50, b"")) or _usable_ipv4(body[12:16])
    elif op == 2 and message_type == 5:  # ACK
        ipv4 = _usable_ipv4(body[16:20])
    hostname = _clean_name(options.get(12, b"")) if op == 1 else None
    if ipv4 is None and hostname is None:
        return ()
    return (
        Observation(
            mac=mac,
            ipv4=ipv4,
            ipv4_rank=RANK_DHCP if ipv4 else 0,
            hostname=hostname,
            hostname_rank=RANK_DHCP if hostname else 0,
        ),
    )


def _dhcp_options(data: bytes) -> dict[int, bytes]:
    options: dict[int, bytes] = {}
    index = 0
    while index < len(data):
        code = data[index]
        if code == 255:
            break
        if code == 0:
            index += 1
            continue
        if index + 1 >= len(data):
            break
        length = data[index + 1]
        value = data[index + 2 : index + 2 + length]
        if len(value) != length:
            break
        options.setdefault(code, value)
        index += 2 + length
    return options


def _parse_mdns(mac: str, message: bytes, *, own_address: str) -> tuple[Observation, ...]:
    """Take ``<name>.local`` from mDNS records for the sender's own address.

    Responses carry them as answers, probes in the authority section. Known
    answers in queries name other hosts, which the address check skips.
    """
    if len(message) < 12:
        return ()
    qdcount, ancount, nscount, arcount = struct.unpack_from("!HHHH", message, 4)
    offset = 12
    try:
        for _ in range(qdcount):
            _, offset = _dns_name(message, offset)
            offset += 4
        for _ in range(ancount + nscount + arcount):
            name, offset = _dns_name(message, offset)
            rtype, _, _, rdlength = struct.unpack_from("!HHIH", message, offset)
            offset += 10
            rdata = message[offset : offset + rdlength]
            offset += rdlength
            address = None
            if rtype == 1 and rdlength == 4:
                address = str(ipaddress.IPv4Address(rdata))
            elif rtype == 28 and rdlength == 16:
                address = str(ipaddress.IPv6Address(rdata))
            if address != own_address or not name.lower().endswith(".local"):
                continue
            hostname = _clean_name(name[: -len(".local")].encode())
            if hostname is None:
                continue
            ipv4 = address if rtype == 1 else None
            return (
                Observation(
                    mac=mac,
                    ipv4=ipv4,
                    ipv4_rank=RANK_MDNS if ipv4 else 0,
                    ipv6=(address,) if rtype == 28 else (),
                    hostname=hostname,
                    hostname_rank=RANK_MDNS,
                ),
            )
    except (struct.error, ValueError, IndexError):
        return ()
    return ()


def _dns_name(message: bytes, offset: int) -> tuple[str, int]:
    labels: list[str] = []
    end = None
    jumps = 0
    while True:
        length = message[offset]
        if length & 0xC0 == 0xC0:
            if end is None:
                end = offset + 2
            offset = ((length & 0x3F) << 8) | message[offset + 1]
            jumps += 1
            if jumps > 16:
                raise ValueError("DNS name compression loop")
            continue
        offset += 1
        if length == 0:
            break
        labels.append(message[offset : offset + length].decode("utf-8", "replace"))
        offset += length
    return ".".join(labels), end if end is not None else offset


def _clean_name(raw: bytes) -> str | None:
    text = raw.split(b"\x00", 1)[0].decode("utf-8", "replace").strip()
    text = "".join(ch for ch in text if ch.isprintable())[:MAX_NAME]
    return text or None


def _mac(raw: bytes) -> str | None:
    if len(raw) != 6 or raw[0] & 0x01 or raw == b"\x00" * 6:
        return None
    return raw.hex(":")


def _usable_ipv4(raw: bytes) -> str | None:
    if len(raw) != 4:
        return None
    address = ipaddress.IPv4Address(raw)
    if address.is_unspecified or address.is_multicast or address == ipaddress.IPv4Address("255.255.255.255"):
        return None
    return str(address)


def _usable_ipv6(raw: bytes) -> str | None:
    if len(raw) != 16:
        return None
    address = ipaddress.IPv6Address(raw)
    if address.is_unspecified or address.is_multicast:
        return None
    return str(address)


# --- learned state -------------------------------------------------------------


@dataclass
class _Entry:
    ipv4: str | None = None
    ipv4_rank: int = 0
    ipv4_seen: float = 0.0
    ipv6: dict[str, float] = field(default_factory=dict)
    hostname: str | None = None
    hostname_rank: int = 0
    hostname_seen: float = 0.0
    last_seen: float = 0.0


class ClientAddressTable:
    """Thread-safe MAC -> address/name table with ageing."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        address_ttl: float = ADDRESS_TTL,
        name_ttl: float = NAME_TTL,
        max_clients: int = MAX_CLIENTS,
    ) -> None:
        self._clock = clock
        self._address_ttl = address_ttl
        self._name_ttl = name_ttl
        self._max_clients = max_clients
        self._entries: dict[str, _Entry] = {}
        self._lock = threading.Lock()

    def observe(self, observations: Iterable[Observation]) -> None:
        now = self._clock()
        with self._lock:
            for observation in observations:
                entry = self._entries.get(observation.mac)
                if entry is None:
                    self._make_room()
                    entry = self._entries[observation.mac] = _Entry()
                entry.last_seen = now
                if observation.ipv4 and _should_replace(
                    entry.ipv4_rank, entry.ipv4_seen, observation.ipv4_rank, now
                ):
                    entry.ipv4 = observation.ipv4
                    entry.ipv4_rank = observation.ipv4_rank
                    entry.ipv4_seen = now
                elif observation.ipv4 == entry.ipv4:
                    entry.ipv4_seen = now
                for address in observation.ipv6:
                    entry.ipv6[address] = now
                if len(entry.ipv6) > MAX_IPV6:
                    newest = sorted(entry.ipv6.items(), key=lambda item: item[1])[-MAX_IPV6:]
                    entry.ipv6 = dict(newest)
                if observation.hostname and _should_replace(
                    entry.hostname_rank, entry.hostname_seen, observation.hostname_rank, now
                ):
                    entry.hostname = observation.hostname
                    entry.hostname_rank = observation.hostname_rank
                    entry.hostname_seen = now
                elif observation.hostname == entry.hostname:
                    entry.hostname_seen = now

    def clients(self) -> tuple[WirelessClientState, ...]:
        now = self._clock()
        result = []
        with self._lock:
            for mac in sorted(self._entries):
                entry = self._entries[mac]
                ipv4 = entry.ipv4 if now - entry.ipv4_seen <= self._address_ttl else None
                ipv6 = tuple(
                    sorted(
                        address
                        for address, seen in entry.ipv6.items()
                        if now - seen <= self._address_ttl
                    )
                )
                hostname = entry.hostname if now - entry.hostname_seen <= self._name_ttl else None
                if ipv4 or ipv6 or hostname:
                    result.append(
                        WirelessClientState(mac=mac, ipv4=ipv4, ipv6=ipv6, hostname=hostname)
                    )
        return tuple(result)

    def _make_room(self) -> None:
        if len(self._entries) < self._max_clients:
            return
        oldest = min(self._entries, key=lambda mac: self._entries[mac].last_seen)
        del self._entries[oldest]


def _should_replace(current_rank: int, current_seen: float, new_rank: int, now: float) -> bool:
    return new_rank >= current_rank or now - current_seen > REPLACE_AFTER


# --- kernel filter -----------------------------------------------------------

# Classic BPF opcodes used below.
_LD_H_ABS = 0x28
_LD_B_ABS = 0x30
_LD_H_IND = 0x48
_LDX_B_MSH = 0xB1
_JEQ = 0x15
_JGE = 0x35
_JGT = 0x25
_JSET = 0x45
_RET = 0x06


def _assemble(program: list[tuple]) -> list[tuple[int, int, int, int]]:
    """Resolve ``(label, code, k, jt_label, jf_label)`` rows to sock_filter tuples."""
    positions = {row[0]: index for index, row in enumerate(program) if row[0]}
    compiled = []
    for index, (_, code, k, jt, jf) in enumerate(program):
        jt_offset = positions[jt] - index - 1 if jt else 0
        jf_offset = positions[jf] - index - 1 if jf else 0
        compiled.append((code, jt_offset, jf_offset, k))
    return compiled


SNOOP_FILTER = _assemble(
    [
        (None, _LD_H_ABS, 12, None, None),
        (None, _JEQ, 0x0806, "accept", None),
        (None, _JEQ, 0x0800, "ipv4", None),
        (None, _JEQ, 0x86DD, "ipv6", "reject"),
        ("ipv4", _LD_B_ABS, 23, None, None),
        (None, _JEQ, 17, None, "reject"),
        (None, _LD_H_ABS, 20, None, None),
        (None, _JSET, 0x1FFF, "reject", None),
        (None, _LDX_B_MSH, 14, None, None),
        (None, _LD_H_IND, 14, None, None),
        (None, _JEQ, 67, "accept", None),
        (None, _JEQ, 68, "accept", None),
        (None, _JEQ, 5353, "accept", "reject"),
        ("ipv6", _LD_B_ABS, 20, None, None),
        (None, _JEQ, 58, None, "udp6"),
        (None, _LD_B_ABS, 54, None, None),
        (None, _JGE, 133, None, "reject"),
        (None, _JGT, 136, "reject", "accept"),
        ("udp6", _JEQ, 17, None, "reject"),
        (None, _LD_H_ABS, 56, None, None),
        (None, _JEQ, 5353, "accept", "reject"),
        ("accept", _RET, 0xFFFF, None, None),
        ("reject", _RET, 0, None, None),
    ]
)


def _attach_filter(sock: socket.socket) -> None:
    import ctypes

    code = b"".join(struct.pack("HBBI", *row) for row in SNOOP_FILTER)
    buffer = ctypes.create_string_buffer(code)
    program = struct.pack("HL", len(SNOOP_FILTER), ctypes.addressof(buffer))
    sock.setsockopt(socket.SOL_SOCKET, SO_ATTACH_FILTER, program)
    # The kernel copies the program during setsockopt; the buffer may go now.


# --- listener ------------------------------------------------------------------


def bridge_interfaces(sys_class_net: Path = Path("/sys/class/net")) -> tuple[str, ...]:
    try:
        return tuple(
            sorted(path.name for path in sys_class_net.iterdir() if (path / "bridge").is_dir())
        )
    except OSError:
        return ()


class BridgeClientSnooper:
    """Background listener that feeds a :class:`ClientAddressTable`."""

    def __init__(
        self,
        interfaces: str = "auto",
        *,
        table: ClientAddressTable | None = None,
        discover: Callable[[], tuple[str, ...]] = bridge_interfaces,
    ) -> None:
        self._interfaces = interfaces.strip()
        self._discover = discover
        self.table = table or ClientAddressTable()
        self._started = False
        self._start_lock = threading.Lock()

    def clients(self) -> tuple[WirelessClientState, ...]:
        self._ensure_started()
        return self.table.clients()

    def _ensure_started(self) -> None:
        with self._start_lock:
            if self._started:
                return
            self._started = True
        if self._interfaces.lower() in {"", "off", "none", "0", "false"}:
            log.info("Client address snooping is off")
            return
        if not hasattr(socket, "AF_PACKET") or os.name != "posix":
            log.debug("Client address snooping needs Linux packet sockets; skipped")
            return
        names = (
            self._discover()
            if self._interfaces.lower() == "auto"
            else tuple(name.strip() for name in self._interfaces.split(",") if name.strip())
        )
        sockets = []
        for name in names:
            try:
                sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(ETH_P_ALL))
                _attach_filter(sock)
                sock.bind((name, 0))
                sock.setblocking(False)
                sockets.append(sock)
            except OSError as exc:
                log.warning("Client address snooping unavailable on %s: %s", name, exc)
        if not sockets:
            return
        log.info("Client address snooping on %s", ", ".join(names))
        threading.Thread(
            target=self._run, args=(sockets,), name="client-snoop", daemon=True
        ).start()

    def _run(self, sockets: list[socket.socket]) -> None:
        while True:
            try:
                ready, _, _ = select.select(sockets, [], [], 5.0)
                for sock in ready:
                    try:
                        frame = sock.recv(65535)
                    except BlockingIOError:
                        continue
                    except OSError as exc:
                        # The bridge goes down briefly on a network reload
                        # (IP Settings, DHCP); the socket works again after.
                        log.debug("Client address snooping: %s", exc)
                        continue
                    self.table.observe(parse_frame(frame))
            except Exception:  # pragma: no cover - keep the agent alive
                log.exception("Client address snooping error")
                time.sleep(5.0)
