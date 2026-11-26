from open_omada_device_agent.adapters.inbound.ecsp.mappers.configuration import (
    parse_config_body,
)
from open_omada_device_agent.adapters.outbound.openwrt.rogue_ap_scan import (
    PM_11AC,
    PM_11AXG,
    PM_11B,
    PM_11NG,
    SEC_NONE,
    SEC_OWE,
    SEC_WEP,
    SEC_WPA2_ENTERPRISE,
    SEC_WPA2_PSK,
    SEC_WPA2_PSK_WPA3_SAE,
    SEC_WPA_WPA2_PSK,
    OpenWrtRogueApScanner,
    channel_for_frequency,
    parse_ap_interfaces,
    parse_scan,
)
from open_omada_device_agent.application.contracts import PlatformCapabilities
from open_omada_device_agent.capabilities import ap_components_v2
from open_omada_device_agent.contexts.wireless.domain import RadioBand
from open_omada_device_agent.openwrt import CommandResult

IW_DEV = """phy#1
\tInterface phy1-ap0
\t\tifindex 12
\t\taddr 02:00:00:00:01:00
\t\tssid lab
\t\ttype AP
\t\tchannel 36 (5180 MHz), width: 80 MHz, center1: 5210 MHz
\t\ttxpower 20.00 dBm
phy#0
\tInterface phy0-ap1
\t\taddr 02:00:00:00:00:01
\t\ttype AP
\t\tchannel 6 (2437 MHz), width: 20 MHz, center1: 2437 MHz
\tInterface phy0-ap0
\t\taddr 02:00:00:00:00:00
\t\ttype AP
\t\tchannel 6 (2437 MHz), width: 20 MHz, center1: 2437 MHz
\tInterface phy0-sta0
\t\taddr 02:00:00:00:00:02
\t\ttype managed
"""

SCAN_2G = """BSS 00:00:5e:00:53:01(on phy0-ap0)
\tlast seen: 10.0s [boottime]
\tfreq: 2412.0
\tbeacon interval: 100 TUs
\tcapability: ESS (0x0431)
\tsignal: -61.00 dBm
\tSSID: neighbour-wpa2
\tRSN:\t * Version: 1
\t\t * Group cipher: CCMP
\t\t * Pairwise ciphers: CCMP
\t\t * Authentication suites: PSK
\tHT capabilities:
\t\tCapabilities: 0x9ed
\tHT operation:
\t\t * primary channel: 1
\tHE capabilities:
\t\tHE MAC Capabilities (0x0)
BSS 00:00:5e:00:53:02(on phy0-ap0)
\tfreq: 2437.0
\tbeacon interval: 102 TUs
\tcapability: ESS (0x0401)
\tsignal: -80.50 dBm
\tSSID: neighbour-mixed
\tSupported rates: 1.0* 2.0* 5.5* 11.0*
\tDS Parameter set: channel 6
\tRSN:\t * Version: 1
\t\t * Authentication suites: PSK SAE
\tHT capabilities:
\t\tCapabilities: 0x1
BSS 00:00:5e:00:53:03(on phy0-ap0)
\tfreq: 2462.0
\tbeacon interval: 100 TUs
\tcapability: ESS (0x0411)
\tsignal: -90.00 dBm
\tSSID: old-wep
\tSupported rates: 1.0* 2.0* 5.5* 11.0*
BSS 00:00:5e:00:53:04(on phy0-ap0)
\tfreq: 2462.0
\tcapability: ESS (0x0401)
\tsignal: -70.00 dBm
\tSSID:
\tWPA:\t * Version: 1
\t\t * Authentication suites: PSK
\tRSN:\t * Version: 1
\t\t * Authentication suites: PSK
\tHT capabilities:
\t\tCapabilities: 0x1
BSS 02:00:00:00:00:01(on phy0-ap0)
\tfreq: 2437.0
\tsignal: -20.00 dBm
\tSSID: our-own-bss
"""

SCAN_5G = """BSS 00:00:5e:00:53:10(on phy1-ap0)
\tfreq: 5180.0
\tbeacon interval: 100 TUs
\tcapability: ESS (0x0111)
\tsignal: -72.00 dBm
\tSSID: office
\tRSN:\t * Version: 1
\t\t * Authentication suites: IEEE 802.1X
\tHT capabilities:
\t\tCapabilities: 0x11ef
\tVHT capabilities:
\t\tVHT Capabilities (0x33c979b1):
BSS 00:00:5e:00:53:11(on phy1-ap0)
\tfreq: 5745.0
\tcapability: ESS (0x0011)
\tsignal: -85.00 dBm
\tSSID: cafe
\tRSN:\t * Version: 1
\t\t * Authentication suites: OWE
"""


class FakeRunner:
    def __init__(self, outputs):
        self.outputs = outputs
        self.calls = []

    def run(self, args, *, input_text=None):
        self.calls.append(tuple(args))
        return self.outputs.get(tuple(args), CommandResult(returncode=1, stderr="no"))


def _caps(**overrides):
    values = {"platform": "openwrt", "supports_rogue_ap_scan": True}
    values.update(overrides)
    return PlatformCapabilities(**values)


def test_channel_for_frequency_covers_all_bands():
    assert channel_for_frequency(2412) == 1
    assert channel_for_frequency(2484) == 14
    assert channel_for_frequency(5180) == 36
    assert channel_for_frequency(5825) == 165
    assert channel_for_frequency(5955) == 1
    assert channel_for_frequency(900) is None


def test_parse_ap_interfaces_skips_non_ap_interfaces():
    interfaces = parse_ap_interfaces(IW_DEV)

    assert [(i.ifname, i.band) for i in interfaces] == [
        ("phy1-ap0", RadioBand.FIVE_G),
        ("phy0-ap1", RadioBand.TWO_G),
        ("phy0-ap0", RadioBand.TWO_G),
    ]
    assert interfaces[0].mac == "02:00:00:00:01:00"


def test_parse_scan_maps_security_mode_and_channel():
    by_ssid = {ap.ssid: ap for ap in parse_scan(SCAN_2G, RadioBand.TWO_G)}

    wpa2 = by_ssid["neighbour-wpa2"]
    assert (wpa2.channel, wpa2.security, wpa2.phy_mode) == (1, SEC_WPA2_PSK, PM_11AXG)
    assert (wpa2.signal, wpa2.beacon_interval) == (-61, 100)

    mixed = by_ssid["neighbour-mixed"]
    assert (mixed.channel, mixed.security, mixed.phy_mode) == (6, SEC_WPA2_PSK_WPA3_SAE, PM_11NG)
    assert mixed.signal == -80

    wep = by_ssid["old-wep"]
    assert (wep.channel, wep.security, wep.phy_mode) == (11, SEC_WEP, PM_11B)

    hidden = by_ssid[""]
    assert hidden.security == SEC_WPA_WPA2_PSK
    assert hidden.beacon_interval is None


def test_parse_scan_5g_enterprise_and_owe():
    by_ssid = {ap.ssid: ap for ap in parse_scan(SCAN_5G, RadioBand.FIVE_G)}

    assert (by_ssid["office"].channel, by_ssid["office"].security) == (36, SEC_WPA2_ENTERPRISE)
    assert by_ssid["office"].phy_mode == PM_11AC
    assert (by_ssid["cafe"].channel, by_ssid["cafe"].security) == (149, SEC_OWE)


def test_open_network_reports_no_security():
    scan = "BSS 00:00:5e:00:53:20(on x)\n\tfreq: 2412.0\n\tcapability: ESS (0x0401)\n\tSSID: open\n"

    assert parse_scan(scan, RadioBand.TWO_G)[0].security == SEC_NONE


def _scanner(clock):
    runner = FakeRunner(
        {
            ("iw", "dev"): CommandResult(0, IW_DEV),
            ("iw", "dev", "phy1-ap0", "scan", "ap-force"): CommandResult(0, SCAN_5G),
            ("iw", "dev", "phy0-ap1", "scan", "ap-force"): CommandResult(0, SCAN_2G),
        }
    )
    started = []
    scanner = OpenWrtRogueApScanner(
        runner,
        clock=lambda: clock[0],
        start_thread=started.append,
    )
    return scanner, runner, started


def test_scan_request_runs_in_background_and_reports_once():
    clock = [100.0]
    scanner, runner, started = _scanner(clock)
    update = parse_config_body({"sequenceId": 3, "rogueApScan": {"type": 0}})

    result = scanner.reconcile(update, _caps())

    assert (result.applied, result.changed) == (True, True)
    assert runner.calls == []  # nothing runs on the session thread
    assert scanner.inform_items() == []

    started[0]()
    scanned = [call for call in runner.calls if call[-1] == "ap-force"]
    assert scanned == [
        ("iw", "dev", "phy1-ap0", "scan", "ap-force"),
        ("iw", "dev", "phy0-ap1", "scan", "ap-force"),
    ]
    items = scanner.inform_items()
    macs = {item["mac"] for item in items}
    assert "02-00-00-00-00-01" not in macs  # our own BSS is not a rogue AP
    assert len(items) == 6
    office = next(item for item in items if item["ssid"] == "office")
    assert office == {
        "mac": "00-00-5E-00-53-10",
        "ssid": "office",
        "rid": 1,
        "ch": 36,
        "sec": SEC_WPA2_ENTERPRISE,
        "pm": PM_11AC,
        "bcn": 100,
        "signal": -72,
    }

    assert scanner.inform_items() == []  # the next inform does not repeat it


def test_second_scan_request_while_running_is_ignored():
    clock = [0.0]
    scanner, _, started = _scanner(clock)
    update = parse_config_body({"sequenceId": 3, "rogueApScan": {"type": 0}})

    scanner.reconcile(update, _caps())
    result = scanner.reconcile(update, _caps())

    assert (result.applied, result.changed) == (True, False)
    assert len(started) == 1


def test_scan_request_fails_cleanly_without_scan_support():
    scanner, runner, started = _scanner([0.0])
    update = parse_config_body({"sequenceId": 3, "rogueApScan": {"type": 0}})

    result = scanner.reconcile(update, _caps(supports_rogue_ap_scan=False))

    assert result.applied is False
    assert started == [] and runner.calls == []


def test_scanner_ignores_updates_without_scan():
    scanner, runner, started = _scanner([0.0])

    result = scanner.reconcile(parse_config_body({"led": {"enable": 1}}), _caps())

    assert (result.applied, result.changed) == (True, False)
    assert started == []


def test_mapper_accepts_rogue_ap_keys():
    update = parse_config_body(
        {
            "sequenceId": 4,
            "rogueApScan": {"type": 0},
            "rogueApGlobal": {"enable": True},
            "rogueApSetting": [{"mac": "00-00-5E-00-53-01", "trust": True}],
        }
    )

    assert update.rogue_ap_scan is True
    assert update.unhandled_keys == ()
    assert update.passive_keys == ("rogueApGlobal", "rogueApSetting")


def test_rogue_ap_components_follow_the_capability():
    assert "rogueAp" not in ap_components_v2(_caps(supports_rogue_ap_scan=False))
    components = ap_components_v2(_caps())
    assert components["rogueAp"] == "3.0"
    assert components["rogueApInform"] == "2.0"
