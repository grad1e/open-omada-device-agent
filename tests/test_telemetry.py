import json
from dataclasses import dataclass

import pytest

from open_omada_device_agent.domain import RadioBand, WirelessClientState
from open_omada_device_agent.contexts.clients.domain import ClientRadioBand
from open_omada_device_agent.openwrt import CommandResult
from open_omada_device_agent.platform_capabilities import PlatformCapabilities
from open_omada_device_agent.telemetry import (
    OpenWrtWirelessInterface,
    OpenWrtWirelessTelemetry,
    collect_openwrt_wireless_clients,
    collect_openwrt_wireless_inform,
    hostapd_client_states,
    openwrt_wireless_interfaces_from_status,
    openwrt_wireless_inform_from_status,
)


# Lab Jio (mt76), radio0, trimmed.
SURVEY_DUMP = """Survey data from phy0-ap0
\tfrequency:\t\t\t2412 MHz
\tnoise:\t\t\t\t-95 dBm
\tchannel active time:\t\t100 ms
\tchannel busy time:\t\t90 ms
\tfrequency:\t\t\t2437 MHz [in use]
\tnoise:\t\t\t\t-92 dBm
\tchannel active time:\t\t1896107 ms
\tchannel busy time:\t\t317026 ms
\tchannel receive time:\t\t298204 ms
\tchannel BSS receive time:\t0 ms
\tchannel transmit time:\t\t7228 ms
\tfrequency:\t\t\t2442 MHz
\tchannel active time:\t\t50 ms
\tchannel busy time:\t\t50 ms
"""


# Lab Jio, radio0 on 2.4 GHz Auto 20/40 after falling back to 20 MHz.
IW_INFO = """Interface phy0-ap0
\tifindex 19
\ttype AP
\twiphy 0
\tchannel 6 (2437 MHz), width: 20 MHz, center1: 2437 MHz
\ttxpower 20.00 dBm
"""


@dataclass
class StaticRunner:
    result: CommandResult
    calls: list[tuple[str, ...]]

    def run(self, args, *, input_text=None):
        assert input_text is None
        self.calls.append(tuple(args))
        return self.result


@dataclass
class SequenceRunner:
    results: list[CommandResult]
    calls: list[tuple[str, ...]]

    def run(self, args, *, input_text=None):
        assert input_text is None
        self.calls.append(tuple(args))
        return self.results.pop(0)


def test_maps_openwrt_wireless_status_to_omada_inform_keys():
    status = {
        "radio0": {
            "config": {
                "channel": "11",
                "htmode": "HT20",
                "hwmode": "11g",
                "txpower": 17,
            },
            "interfaces": [
                {
                    "config": {"ssid": "guest"},
                    "bssid": "02:00:00:00:00:10",
                    "stations": {"aa:bb:cc:dd:ee:ff": {}},
                    "statistics": {
                        "tx_bytes": "1234",
                        "rx_bytes": "567",
                        "tx_packets": 12,
                        "rx_packets": 7,
                    },
                }
            ],
        },
        "radio1": {
            "config": {"channel": 36, "htmode": "VHT80"},
            "interfaces": [{"config": {"ssid": "corp"}, "num_sta": 2}],
        },
    }

    payload = openwrt_wireless_inform_from_status(status)

    assert payload["wSettings_2G"] == {
        "ch": "11",
        "bw": "HT20",
        "rdMode": "b/g/n mixed",
        "txPower": "17dBm",
        "staNum": 1,
        "busyUti": 0,
        "txUti": 0,
        "rxUti": 0,
        "interUti": 0,
    }
    assert payload["ssidStats_2G"] == [
        {
            "id": -1,
            "ssid": "guest",
            "clntNum": 1,
            "bssid": "02-00-00-00-00-10",
            "down": 1234,
            "up": 567,
            "downPkts": 12,
            "upPkts": 7,
        }
    ]
    assert payload["wSettings_5G"] == {
        "ch": "36",
        "bw": "VHT80",
        "rdMode": "a/n/ac mixed",
        "staNum": 2,
        "busyUti": 0,
        "txUti": 0,
        "rxUti": 0,
        "interUti": 0,
    }
    assert payload["ssidStats_5G"] == [{"id": -1, "ssid": "corp", "clntNum": 2}]


@pytest.mark.parametrize(
    ("txpower", "expected"),
    [(20, "20dBm"), ("20", "20dBm"), ("23dBm", "23dBm"), ("17.0", "17dBm")],
)
def test_wireless_inform_formats_tx_power_with_dbm_unit(txpower, expected):
    status = {"radio0": {"config": {"channel": "1", "txpower": txpower}, "interfaces": []}}

    payload = openwrt_wireless_inform_from_status(status)

    assert payload["wSettings_2G"]["txPower"] == expected


@pytest.mark.parametrize(("channel", "expected"), [("auto", "0"), ("149", "149"), (36, "36")])
def test_wireless_inform_reports_auto_channel_as_zero(channel, expected):
    status = {"radio1": {"config": {"channel": channel}, "interfaces": []}}

    payload = openwrt_wireless_inform_from_status(status)

    assert payload["wSettings_5G"]["ch"] == expected


def test_wireless_inform_omits_unparseable_tx_power():
    status = {"radio0": {"config": {"channel": "1", "txpower": "auto"}, "interfaces": []}}

    payload = openwrt_wireless_inform_from_status(status)

    assert "txPower" not in payload["wSettings_2G"]


def test_collect_openwrt_wireless_telemetry_uses_ubus_without_shell():
    runner = StaticRunner(
        result=CommandResult(
            returncode=0,
            stdout=json.dumps({"radio0": {"config": {"channel": 6}, "interfaces": []}}),
        ),
        calls=[],
    )
    caps = PlatformCapabilities(platform="openwrt", has_ubus=True)

    payload = collect_openwrt_wireless_inform(capabilities=caps, runner=runner)

    assert payload == {
        "wSettings_2G": {"ch": "6", "busyUti": 0, "txUti": 0, "rxUti": 0, "interUti": 0}
    }
    assert runner.calls == [
        ("ubus", "call", "network.wireless", "status"),
        ("uci", "-q", "show", "wireless"),
    ]


def test_collect_openwrt_wireless_telemetry_uses_hostapd_for_ssid_client_counts():
    runner = SequenceRunner(
        results=[
            CommandResult(
                returncode=0,
                stdout=json.dumps(
                    {
                        "radio0": {
                            "config": {"channel": 6, "htmode": "HT20"},
                            "interfaces": [
                                {
                                    "ifname": "phy0-ap0",
                                    "section": "openomada_2g_1",
                                    "config": {"ssid": "guest"},
                                    "stations": [],
                                }
                            ],
                        }
                    }
                ),
            ),
            CommandResult(
                returncode=0,
                stdout="wireless.openomada_2g_1.openomada_ssid_id='180126366'\n",
            ),
            CommandResult(
                returncode=0,
                stdout=json.dumps(
                    {
                        "clients": {
                            "aa:bb:cc:dd:ee:ff": {
                                "bytes": {"rx": 100, "tx": 200},
                                "packets": {"rx": 1, "tx": 2},
                            }
                        }
                    }
                ),
            ),
            CommandResult(returncode=0, stdout=IW_INFO),
            CommandResult(returncode=0, stdout=SURVEY_DUMP),
        ],
        calls=[],
    )
    caps = PlatformCapabilities(platform="openwrt", has_ubus=True)

    payload = collect_openwrt_wireless_inform(capabilities=caps, runner=runner)

    assert payload["wSettings_2G"] == {
        "ch": "6",
        "bw": "20MHz",
        "rdMode": "b/g/n mixed",
        "staNum": 1,
        "busyUti": 17,
        "txUti": 0,
        "rxUti": 16,
        "interUti": 1,
    }
    assert payload["ssidStats_2G"] == [
        {
            "id": 180126366,
            "ssid": "guest",
            "clntNum": 1,
            "down": 200,
            "up": 100,
            "downPkts": 2,
            "upPkts": 1,
        }
    ]
    assert payload["radioTraffic_2G"] == {
        "tx": 200,
        "rx": 100,
        "txP": 2,
        "rxP": 1,
        "txDP": 0,
        "rxDP": 0,
        "txEP": 0,
        "rxEP": 0,
        "txRP": 0,
        "rxRP": 0,
    }
    assert runner.calls == [
        ("ubus", "call", "network.wireless", "status"),
        ("uci", "-q", "show", "wireless"),
        ("ubus", "call", "hostapd.phy0-ap0", "get_clients"),
        ("iw", "dev", "phy0-ap0", "info"),
        ("iw", "dev", "phy0-ap0", "survey", "dump"),
    ]


def test_collect_openwrt_wireless_telemetry_is_empty_without_ubus():
    runner = StaticRunner(result=CommandResult(returncode=0, stdout="{}"), calls=[])
    caps = PlatformCapabilities(platform="openwrt", has_ubus=False)

    assert collect_openwrt_wireless_inform(capabilities=caps, runner=runner) == {}
    assert runner.calls == []


def test_extracts_hostapd_interfaces_from_openwrt_wireless_status():
    interfaces = openwrt_wireless_interfaces_from_status(
        {
            "radio0": {
                "interfaces": [
                    {"ifname": "wlan0", "config": {"ssid": "guest"}},
                    {"section": "wlan0-1", "config": {"ssid": "iot"}},
                ]
            }
        }
    )

    assert interfaces == (
        OpenWrtWirelessInterface(ifname="wlan0", ssid="guest", band=RadioBand.TWO_G),
        OpenWrtWirelessInterface(ifname="wlan0-1", ssid="iot", band=RadioBand.TWO_G),
    )


def test_maps_hostapd_clients_to_wireless_client_state():
    clients = hostapd_client_states(
        OpenWrtWirelessInterface(ifname="wlan0", ssid="guest", band=RadioBand.TWO_G),
        {
            "clients": {
                "AA-BB-CC-DD-EE-FF": {
                    "signal": -61,
                    "snr": 35,
                    "bytes": {"tx": 1200, "rx": 345},
                    "packets": {"tx": 12, "rx": 5},
                    "rate": {"tx": 7200, "rx": 6500},
                    "connected_time": 42,
                }
            }
        },
    )

    assert len(clients) == 1
    assert clients[0].mac == "aa:bb:cc:dd:ee:ff"
    assert clients[0].ssid == "guest"
    assert clients[0].radio is ClientRadioBand.TWO_G
    assert clients[0].rssi == -61
    assert clients[0].snr == 35
    assert clients[0].rx_bytes == 1200
    assert clients[0].tx_bytes == 345
    assert clients[0].tx_packets == 12
    assert clients[0].rx_packets == 5
    assert clients[0].tx_rate == 7200
    assert clients[0].rx_rate == 6500
    assert clients[0].association_time == 42


def test_collect_openwrt_wireless_clients_discovers_and_queries_hostapd():
    runner = SequenceRunner(
        results=[
            CommandResult(
                returncode=0,
                stdout=json.dumps(
                    {"radio0": {"interfaces": [{"ifname": "wlan0", "config": {"ssid": "guest"}}]}}
                ),
            ),
            CommandResult(
                returncode=0,
                stdout=json.dumps({"clients": {"aa:bb:cc:dd:ee:ff": {"signal": -60}}}),
            ),
        ],
        calls=[],
    )
    caps = PlatformCapabilities(platform="openwrt", has_ubus=True)

    clients = collect_openwrt_wireless_clients(capabilities=caps, runner=runner)

    assert [client.mac for client in clients] == ["aa:bb:cc:dd:ee:ff"]
    assert runner.calls == [
        ("ubus", "call", "network.wireless", "status"),
        ("ubus", "call", "hostapd.wlan0", "get_clients"),
    ]


def test_openwrt_wireless_telemetry_ignores_invalid_json():
    runner = StaticRunner(result=CommandResult(returncode=0, stdout="{not-json"), calls=[])

    assert OpenWrtWirelessTelemetry(runner).collect() == {}


def test_inform_projection_includes_wireless_telemetry():
    from open_omada_device_agent.projections.inform import InformAssembler, LanObservation
    assembler = InformAssembler(
        device_info=lambda: {"model": "test"},
        lan=LanObservation(100.0, 1, "LAN"),
        clients=lambda: (),
        client_projection=lambda clients: [],
        wireless_projection=lambda: {"wSettings_2G": {"ch": "11"}},
    )
    body = assembler.build(need_reply=False, uptime=0)
    assert body["wSettings_2G"] == {"ch": "11"}


def test_inform_projection_accepts_merged_dhcp_and_hostapd_clients(tmp_path):
    from open_omada_device_agent.client_tracking import client_stats_payload, clients_from_dhcp_leases, load_dnsmasq_leases, merge_wireless_client_states
    from open_omada_device_agent.projections.inform import InformAssembler, LanObservation
    lease_file = tmp_path / "leases"
    lease_file.write_text("1000 aa:bb:cc:dd:ee:ff 192.0.2.10 phone *\n", encoding="utf-8")
    clients = merge_wireless_client_states(
        clients_from_dhcp_leases(load_dnsmasq_leases(str(lease_file))),
        (WirelessClientState(mac="aa:bb:cc:dd:ee:ff", ssid="guest", radio=ClientRadioBand.TWO_G, rssi=-59),),
    )
    assembler = InformAssembler(
        device_info=lambda: {"model": "test"},
        lan=LanObservation(100.0, 1, "LAN"),
        clients=lambda: clients,
        client_projection=client_stats_payload,
        wireless_projection=lambda: {},
    )
    body = assembler.build(need_reply=False, uptime=0)
    client = body["clients"][0]
    assert client["mac"] == "AA-BB-CC-DD-EE-FF"
    assert client["ip"] == "192.0.2.10"
    assert client["name"] == "phone"
    assert client["ssid"] == "guest"
    assert client["rid"] == 0
    assert client["rssi"] == -59


@pytest.mark.parametrize(("rate", "expected"), [(100.0, "100"), (1000.0, "1000"), (2500, "2500")])
def test_inform_projection_reports_lan_rate_as_integer_string(rate, expected):
    from open_omada_device_agent.projections.inform import InformAssembler, LanObservation
    assembler = InformAssembler(
        device_info=lambda: {"model": "test"},
        lan=LanObservation(rate, 1, "LAN"),
        clients=lambda: (),
        client_projection=lambda clients: [],
        wireless_projection=lambda: {},
    )

    body = assembler.build(need_reply=False, uptime=0)

    assert body["lanInfo"]["rate"] == expected


def test_parse_in_use_survey_reads_only_the_in_use_channel():
    from open_omada_device_agent.adapters.outbound.openwrt.telemetry import (
        ChannelSurvey,
        parse_in_use_survey,
    )

    assert parse_in_use_survey(SURVEY_DUMP) == ChannelSurvey(
        frequency=2437, active=1896107, busy=317026, receive=298204, transmit=7228
    )
    assert parse_in_use_survey("Survey data from phy0-ap0\n") is None


def test_channel_utilization_uses_the_delta_between_informs():
    from open_omada_device_agent.adapters.outbound.openwrt.telemetry import (
        ChannelSurvey,
        channel_utilization,
    )

    before = ChannelSurvey(5180, active=10_000, busy=1_000, receive=500, transmit=200)
    after = ChannelSurvey(5180, active=13_000, busy=2_500, receive=900, transmit=1_100)

    assert channel_utilization(before, after) == {
        "busyUti": 50,
        "txUti": 30,
        "rxUti": 13,
        "interUti": 7,
    }


def test_channel_utilization_restarts_after_a_channel_change():
    from open_omada_device_agent.adapters.outbound.openwrt.telemetry import (
        ChannelSurvey,
        channel_utilization,
    )

    before = ChannelSurvey(5180, active=900_000, busy=800_000, receive=0, transmit=0)
    after = ChannelSurvey(5745, active=1_000, busy=100, receive=50, transmit=50)

    assert channel_utilization(before, after)["busyUti"] == 10


def test_parse_uci_ssid_ids_maps_sections_to_controller_ids():
    from open_omada_device_agent.adapters.outbound.openwrt.telemetry import (
        parse_uci_ssid_ids,
    )

    text = (
        "wireless.openomada_5g_1=wifi-iface\n"
        "wireless.openomada_5g_1.openomada_ssid_id='180126366'\n"
        "wireless.default_radio0.ssid='OpenWrt'\n"
    )

    assert parse_uci_ssid_ids(text) == {"openomada_5g_1": 180126366}


def test_hostapd_only_ssid_stats_still_carry_an_id():
    payload: dict[str, object] = {}
    from open_omada_device_agent.adapters.outbound.openwrt.telemetry import (
        _upsert_ssid_stats,
    )

    _upsert_ssid_stats(payload, "5G", {"ssid": "corp", "clntNum": 1})

    assert payload["ssidStats_5G"] == [{"id": -1, "ssid": "corp", "clntNum": 1}]


def test_parse_operating_channel_reads_channel_and_width():
    from open_omada_device_agent.adapters.outbound.openwrt.telemetry import (
        parse_operating_channel,
    )

    assert parse_operating_channel(IW_INFO) == {"ch": "6", "bw": "20MHz"}
    assert parse_operating_channel("Interface phy1-ap0\n\ttype AP\n") == {}


@pytest.mark.parametrize(
    ("radio", "htmode", "expected"),
    [
        ("radio0", "HE40", "b/g/n/ax mixed"),
        ("radio1", "HE80", "a/n/ac/ax mixed"),
        ("radio1", "VHT80", "a/n/ac mixed"),
        ("radio0", "HT20", "b/g/n mixed"),
        ("radio1", "EHT160", "a/n/ac/ax/be mixed"),
    ],
)
def test_wireless_inform_reports_radio_mode_like_a_real_eap(radio, htmode, expected):
    status = {radio: {"config": {"channel": "auto", "htmode": htmode}, "interfaces": []}}

    payload = openwrt_wireless_inform_from_status(status)

    suffix = "2G" if radio == "radio0" else "5G"
    assert payload[f"wSettings_{suffix}"]["rdMode"] == expected
