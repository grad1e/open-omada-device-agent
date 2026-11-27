"""Radios are matched by their UCI ``band``, not by their number.

Many boards have radio0 = 5 GHz; the controller's radioId 0 is always 2.4 GHz.
"""
import json
from dataclasses import dataclass, field

from open_omada_device_agent.adapters.inbound.ecsp.mappers.configuration import parse_config_body
from open_omada_device_agent.adapters.outbound.openwrt.telemetry import (
    OpenWrtWirelessTelemetry,
    openwrt_wireless_inform_from_status,
    openwrt_wireless_interfaces_from_status,
)
from open_omada_device_agent.adapters.outbound.openwrt.uci import (
    CommandResult,
    OpenWrtUciAdapter,
    assign_radio_bands,
    build_uci_batch,
    parse_uci_show,
    radio_sections_by_band,
)
from open_omada_device_agent.application.contracts import PlatformCapabilities
from open_omada_device_agent.contexts.wireless.domain import RadioBand

# A board whose first radio is the 5 GHz one.
SWAPPED_SHOW = """wireless.radio0=wifi-device
wireless.radio0.band='5g'
wireless.radio0.htmode='HE80'
wireless.radio0.openomada_rates='1'
wireless.radio1=wifi-device
wireless.radio1.band='2g'
wireless.radio1.htmode='HE20'
wireless.radio1.openomada_uapsd='0'
wireless.default_radio0=wifi-iface
wireless.default_radio0.device='radio0'
wireless.default_radio1=wifi-iface
wireless.default_radio1.device='radio1'
"""
SWAPPED = parse_uci_show(SWAPPED_SHOW)


def _caps():
    return PlatformCapabilities(
        platform="openwrt",
        radio_bands=(RadioBand.TWO_G, RadioBand.FIVE_G),
        max_ssids=4,
        supports_wlan_config=True,
        supports_wpa2_psk=True,
    )


def _ssid(index, rate_ctl=0):
    return {"id": 1000 + index, "index": index, "ssidName": f"lab{index}", "pskKey": "network-passphrase",
            "pskVer": 2, "rateCtl": rate_ctl, "manageRateCtl": 0}


def test_bands_come_from_the_uci_band_option():
    assert radio_sections_by_band(SWAPPED) == {RadioBand.FIVE_G: "radio0", RadioBand.TWO_G: "radio1"}


def test_second_5ghz_radio_is_5ghz_2_sorted_by_name():
    radios = assign_radio_bands({"radio10": "5g", "radio2": "5g", "radio0": "2g", "radio3": "6g"})

    assert radios == {
        RadioBand.TWO_G: "radio0",
        RadioBand.FIVE_G: "radio2",
        RadioBand.FIVE_G2: "radio10",
        RadioBand.SIX_G: "radio3",
    }


def test_radio_settings_go_to_the_radio_with_that_band():
    update = parse_config_body(
        {
            "wirelessBasic_2G": {"radioId": 0, "channel": 6, "chanWidth": 2},
            "wirelessBasic_5G": {"radioId": 1, "channel": 36, "chanWidth": 5},
        }
    )

    batch = build_uci_batch(update, _caps(), wireless_state=SWAPPED)

    assert "set wireless.radio1.channel='6'" in batch
    assert "set wireless.radio1.htmode='HT20'" in batch
    assert "set wireless.radio0.channel='36'" in batch
    assert "set wireless.radio0.htmode='VHT80'" in batch


def test_ssids_default_iface_uapsd_and_rate_control_use_the_mapped_radio():
    update = parse_config_body(
        {
            "ssid_2G": {"radioId": 0, "ssid": [_ssid(1, 0x30F00)]},  # 24 Mbps minimum
            "ssid_5G": {"radioId": 1, "ssid": [_ssid(2)]},
        }
    )

    batch = build_uci_batch(update, _caps(), wireless_state=SWAPPED)

    assert "set wireless.openomada_2g_1.device='radio1'" in batch
    assert "set wireless.openomada_5g_2.device='radio0'" in batch
    assert "set wireless.default_radio1.disabled='1'" in batch
    assert "set wireless.default_radio0.disabled='1'" in batch
    # U-APSD saved on the 2.4 GHz radio (radio1) reaches the 2.4 GHz SSID only.
    assert "set wireless.openomada_2g_1.uapsd='0'" in batch
    assert not any("openomada_5g_2.uapsd" in line for line in batch)
    assert "set wireless.radio1.openomada_rates='1'" in batch
    # 5 GHz SSID without rate control clears what the agent set on radio0.
    assert "delete wireless.radio0.openomada_rates" in batch


def test_radio_advanced_uses_the_mapped_radio():
    update = parse_config_body({"loadBalance_2G": {"radioId": 0, "enable": 1, "maxAssocClient": 20}})

    batch = build_uci_batch(update, _caps(), wireless_state=SWAPPED)

    assert "set wireless.radio1.maxassoc='20'" in batch
    assert not any("radio0" in line for line in batch)


@dataclass
class _UciRunner:
    show: str
    calls: list[tuple[tuple[str, ...], str | None]] = field(default_factory=list)

    def run(self, args, *, input_text=None):
        args = tuple(args)
        self.calls.append((args, input_text))
        if args == ("uci", "-q", "show", "wireless"):
            return CommandResult(0, self.show, "")
        if args[:3] == ("uci", "-q", "get"):
            section = args[3].split(".")[1]
            return CommandResult(0, parse_uci_show(self.show)[section]["htmode"] + "\n", "")
        return CommandResult(0, "", "")


def test_radio_only_set_reads_uci_and_writes_the_mapped_radio():
    runner = _UciRunner(SWAPPED_SHOW)
    update = parse_config_body({"wirelessBasic_2G": {"radioId": 0, "chanWidth": 3}})

    result = OpenWrtUciAdapter(runner).reconcile(update, _caps())

    assert result.applied is True
    commands = [args for args, _ in runner.calls]
    assert ("uci", "-q", "get", "wireless.radio1.htmode") in commands
    batch = next(text for args, text in runner.calls if args == ("uci", "-q", "batch"))
    assert "set wireless.radio1.htmode='HE40'" in batch
    assert "radio0" not in batch


def test_band_without_a_radio_is_refused_when_radios_have_bands():
    runner = _UciRunner("wireless.radio0=wifi-device\nwireless.radio0.band='5g'\n")
    update = parse_config_body({"wirelessBasic_2G": {"radioId": 0, "channel": 6}})

    result = OpenWrtUciAdapter(runner).reconcile(update, _caps())

    assert result.applied is False
    assert "no wifi-device with the 2g band" in result.error
    assert not any(args == ("uci", "-q", "batch") for args, _ in runner.calls)


def test_without_band_options_radio_numbers_are_used():
    update = parse_config_body({"wirelessBasic_2G": {"radioId": 0, "channel": 6}})
    no_bands = {"radio0": {".type": "wifi-device"}, "radio1": {".type": "wifi-device"}}

    assert "set wireless.radio0.channel='6'" in build_uci_batch(update, _caps(), wireless_state=no_bands)


SWAPPED_STATUS = {
    "radio0": {
        "config": {"band": "5g", "channel": 36, "htmode": "HE80"},
        "interfaces": [{"ifname": "phy0-ap0", "config": {"ssid": "lab5"}, "num_sta": 3}],
    },
    "radio1": {
        "config": {"band": "2g", "channel": 6, "htmode": "HE20"},
        "interfaces": [{"ifname": "phy1-ap0", "config": {"ssid": "lab2"}, "num_sta": 1}],
    },
}


def test_telemetry_reports_radios_under_their_config_band():
    payload = openwrt_wireless_inform_from_status(SWAPPED_STATUS)

    assert payload["wSettings_5G"]["ch"] == "36"
    assert payload["wSettings_5G"]["staNum"] == 3
    assert payload["ssidStats_5G"][0]["ssid"] == "lab5"
    assert payload["wSettings_2G"]["ch"] == "6"
    assert payload["ssidStats_2G"][0]["ssid"] == "lab2"


def test_client_interfaces_carry_the_config_band():
    bands = {item.ifname: item.band for item in openwrt_wireless_interfaces_from_status(SWAPPED_STATUS)}

    assert bands == {"phy0-ap0": RadioBand.FIVE_G, "phy1-ap0": RadioBand.TWO_G}


def test_name_guess_never_takes_a_band_another_radio_has():
    status = {
        "radio0": {"config": {"band": "5g", "channel": 36}, "interfaces": []},
        "radio1": {"config": {"channel": 40}, "interfaces": []},  # no band: guess says 5 GHz
    }

    payload = openwrt_wireless_inform_from_status(status)

    assert payload["wSettings_5G"]["ch"] == "36"
    assert "wSettings_2G" not in payload


def test_radio_without_band_is_left_out_when_others_have_one():
    # The UCI adapter refuses 5 GHz settings here (no radio has band 5g), so
    # telemetry must not report radio1 as 5 GHz either.
    status = {
        "radio0": {"config": {"band": "2g", "channel": 6}, "interfaces": []},
        "radio1": {"config": {"channel": 36}, "interfaces": []},
    }

    payload = openwrt_wireless_inform_from_status(status)

    assert payload["wSettings_2G"]["ch"] == "6"
    assert "wSettings_5G" not in payload


def test_radio_names_are_used_when_no_radio_has_a_band():
    status = {
        "radio0": {"config": {"channel": 6}, "interfaces": []},
        "radio1": {"config": {"channel": 36}, "interfaces": []},
    }

    payload = openwrt_wireless_inform_from_status(status)

    assert payload["wSettings_2G"]["ch"] == "6"
    assert payload["wSettings_5G"]["ch"] == "36"


class _TelemetryRunner:
    def __init__(self):
        self.calls = []

    def run(self, args, *, input_text=None):
        args = tuple(args)
        self.calls.append(args)
        if args == ("ubus", "call", "network.wireless", "status"):
            return CommandResult(0, json.dumps(SWAPPED_STATUS), "")
        if args == ("iw", "dev", "phy0-ap0", "info"):
            return CommandResult(0, "Interface phy0-ap0\n\tchannel 40 (5200 MHz), width: 40 MHz\n", "")
        return CommandResult(1, "", "")


def test_live_channel_is_merged_into_the_band_of_the_radio():
    payload = OpenWrtWirelessTelemetry(_TelemetryRunner()).collect()

    assert payload["wSettings_5G"]["ch"] == "40"
    assert payload["wSettings_2G"]["ch"] == "6"


def test_5ghz_2_settings_and_ssids_go_to_the_second_5ghz_radio():
    state = parse_uci_show(
        "wireless.radio0=wifi-device\nwireless.radio0.band='5g'\n"
        "wireless.radio1=wifi-device\nwireless.radio1.band='2g'\n"
        "wireless.radio2=wifi-device\nwireless.radio2.band='5g'\n"
    )
    update = parse_config_body(
        {
            "wirelessBasic_5G": {"radioId": 1, "channel": 36},
            "wirelessBasic_5G2": {"radioId": 2, "channel": 149},
            "ssid_5G2": {"radioId": 2, "ssid": [_ssid(3)]},
        }
    )

    batch = build_uci_batch(update, _caps(), wireless_state=state)

    assert "set wireless.radio0.channel='36'" in batch
    assert "set wireless.radio2.channel='149'" in batch
    assert "set wireless.openomada_5g2_3.device='radio2'" in batch
