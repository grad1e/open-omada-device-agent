"""802.11 Rate Control (SSID editor) -> per-radio supported/basic rates."""
from open_omada_device_agent.adapters.outbound.openwrt.uci import OpenWrtUciAdapter
from open_omada_device_agent.ap_config import parse_config_body
from open_omada_device_agent.contexts.wireless.domain import RateControl
from open_omada_device_agent.domain import RadioBand
from open_omada_device_agent.openwrt import CommandResult, build_uci_batch
from open_omada_device_agent.platform_capabilities import PlatformCapabilities

# Observed codes (lab, Controller 6.3.0.111).
MIN_24_REQUIRE = 0x30F00  # 2.4 GHz: 24 Mbps minimum, CCK off, require clients
MIN_24_BEACON_1M = 0x50F00  # same without require, beacons at 1 Mbps
MANAGE_18 = 0x10080  # management rate 18 Mbps
MIN_36 = 0x10E00  # 5 GHz: 36 Mbps minimum


def _caps():
    return PlatformCapabilities(
        platform="openwrt",
        radio_bands=(RadioBand.TWO_G, RadioBand.FIVE_G),
        max_ssids=4,
        supports_wlan_config=True,
        supports_wpa2_psk=True,
    )


def _ssid(index, rate_ctl=0, manage=0, name="lab"):
    return {"id": 1000 + index, "index": index, "ssidName": f"{name}{index}", "pskKey": "network-passphrase",
            "pskVer": 2, "rateCtl": rate_ctl, "manageRateCtl": manage}


def _body(*items, key="ssid_2G", radio_id=0):
    return {key: {"radioId": radio_id, "ssid": list(items)}}


def _rate_lines(batch, section="radio0"):
    return [line for line in batch if line.startswith(("add_list", "set", "delete")) and f".{section}." in line
            and any(k in line for k in ("rates", "basic_rate", "beacon_rate", "openomada_rates"))]


def test_codes_decode():
    control = RateControl.from_codes(MIN_24_REQUIRE)
    assert control.enabled and control.require_minimum and not control.beacon_1mbps
    assert control.rates == (24000, 36000, 48000, 54000)

    beacon = RateControl.from_codes(MIN_24_BEACON_1M, MANAGE_18)
    assert beacon.beacon_1mbps and not beacon.require_minimum and beacon.management_rate == 18000

    assert not RateControl.from_codes(0).enabled


def test_minimum_rate_with_require():
    batch = build_uci_batch(parse_config_body(_body(_ssid(1, MIN_24_REQUIRE))), _caps())

    assert _rate_lines(batch) == [
        "delete wireless.radio0.supported_rates",
        "delete wireless.radio0.basic_rate",
        "delete wireless.radio0.beacon_rate",
        "add_list wireless.radio0.supported_rates='24000'",
        "add_list wireless.radio0.supported_rates='36000'",
        "add_list wireless.radio0.supported_rates='48000'",
        "add_list wireless.radio0.supported_rates='54000'",
        "add_list wireless.radio0.basic_rate='24000'",
        "add_list wireless.radio0.basic_rate='36000'",
        "add_list wireless.radio0.basic_rate='48000'",
        "add_list wireless.radio0.basic_rate='54000'",
        "set wireless.radio0.openomada_rates='1'",
    ]


def test_management_rate_and_1mbps_beacons():
    batch = build_uci_batch(parse_config_body(_body(_ssid(1, MIN_24_BEACON_1M, MANAGE_18))), _caps())
    lines = _rate_lines(batch)

    assert "add_list wireless.radio0.supported_rates='18000'" in lines
    assert [line for line in lines if "basic_rate=" in line] == ["add_list wireless.radio0.basic_rate='18000'"]
    assert "set wireless.radio0.beacon_rate='10'" in lines


def test_without_require_only_the_lowest_rate_is_basic():
    batch = build_uci_batch(parse_config_body(_body(_ssid(1, MIN_36), key="ssid_5G", radio_id=1)), _caps())

    assert [line for line in _rate_lines(batch, "radio1") if "basic_rate=" in line] == [
        "add_list wireless.radio1.basic_rate='36000'"
    ]


def test_cck_rates_and_1mbps_beacons_are_dropped_on_5ghz():
    code = 0x40000 | 0x10000 | 0xFFF  # every rate, beacons at 1 Mbps
    batch = build_uci_batch(parse_config_body(_body(_ssid(1, code), key="ssid_5G", radio_id=1)), _caps())
    lines = _rate_lines(batch, "radio1")

    assert "add_list wireless.radio1.supported_rates='1000'" not in lines
    assert "add_list wireless.radio1.supported_rates='6000'" in lines
    assert not any("beacon_rate=" in line for line in lines)


def test_rate_control_off_removes_only_agent_options():
    owned = {"radio0": {".type": "wifi-device", "openomada_rates": "1", "supported_rates": "24000"}}
    batch = build_uci_batch(parse_config_body(_body(_ssid(1))), _caps(), wireless_state=owned)
    assert _rate_lines(batch) == [
        "delete wireless.radio0.supported_rates",
        "delete wireless.radio0.basic_rate",
        "delete wireless.radio0.beacon_rate",
        "delete wireless.radio0.openomada_rates",
    ]

    own_setting = {"radio0": {".type": "wifi-device", "supported_rates": "24000"}}
    batch = build_uci_batch(parse_config_body(_body(_ssid(1))), _caps(), wireless_state=own_setting)
    assert _rate_lines(batch) == []


def test_ssids_that_disagree_leave_the_radio_on_defaults():
    owned = {"radio0": {".type": "wifi-device", "openomada_rates": "1"}}
    body = _body(_ssid(1, MIN_24_REQUIRE), _ssid(2, 0))

    batch = build_uci_batch(parse_config_body(body), _caps(), wireless_state=owned)

    assert "delete wireless.radio0.openomada_rates" in batch
    assert not any("add_list wireless.radio0" in line for line in batch)


def test_ssids_that_agree_share_the_setting():
    batch = build_uci_batch(parse_config_body(_body(_ssid(1, MIN_24_REQUIRE), _ssid(2, MIN_24_REQUIRE))), _caps())

    assert "set wireless.radio0.openomada_rates='1'" in batch


class _Runner:
    def run(self, args, *, input_text=None):
        return CommandResult(0, "", "")


def test_unknown_bits_are_refused():
    adapter = OpenWrtUciAdapter(_Runner())
    result = adapter.reconcile(parse_config_body({"configVersionInc": 1, **_body(_ssid(1, 0x80000 | MIN_36))}), _caps())

    assert not result.applied and "Rate Control bits" in result.error


def test_cck_management_rate_is_refused_on_5ghz():
    adapter = OpenWrtUciAdapter(_Runner())
    body = _body(_ssid(1, MIN_36, 0x10001), key="ssid_5G", radio_id=1)  # management 1 Mbps

    result = adapter.reconcile(parse_config_body({"configVersionInc": 1, **body}), _caps())

    assert not result.applied and "management rate 1000" in result.error


def test_5ghz_management_rate_in_band_is_used():
    body = _body(_ssid(1, MIN_36, 0x10040), key="ssid_5G", radio_id=1)  # management 12 Mbps

    lines = _rate_lines(build_uci_batch(parse_config_body(body), _caps()), "radio1")

    assert "add_list wireless.radio1.supported_rates='12000'" in lines
    assert [line for line in lines if "basic_rate=" in line] == ["add_list wireless.radio1.basic_rate='12000'"]


def test_band_without_ssids_uses_the_radio_with_that_band():
    # A board whose 5 GHz radio is radio0.
    state = {
        "radio0": {".type": "wifi-device", "band": "5g", "openomada_rates": "1"},
        "radio1": {".type": "wifi-device", "band": "2g", "openomada_rates": "1"},
    }
    batch = build_uci_batch(
        parse_config_body({"ssid_5G": {"radioId": 0, "ssid": []}}), _caps(), wireless_state=state
    )

    assert "delete wireless.radio0.openomada_rates" in batch
    assert not any("radio1" in line for line in batch)


def test_two_radios_on_one_band_are_set_separately():
    body = {"ssid_5G": {"radioId": 1, "ssid": [_ssid(1, MIN_36)]}}
    other = {"ssid_5G": {"radioId": 2, "ssid": [_ssid(2, MIN_36)]}}
    first = build_uci_batch(parse_config_body(body), _caps())
    second = build_uci_batch(parse_config_body(other), _caps())

    assert "set wireless.radio1.openomada_rates='1'" in first
    assert "set wireless.radio2.openomada_rates='1'" in second


def test_missing_or_bad_rate_ctl_is_ignored():
    missing = dict(_ssid(1))
    del missing["rateCtl"]
    bad = dict(_ssid(1), rateCtl="")
    words = dict(_ssid(1), rateCtl="fast")

    for item in (missing, bad, words):
        update = parse_config_body(_body(item))
        assert update.wlans[0].rate_control is None
        assert _rate_lines(build_uci_batch(update, _caps())) == []


def test_failed_uci_show_warns_that_rates_stay(caplog):
    class _NoShow(_Runner):
        def run(self, args, *, input_text=None):
            if args[:3] == ["uci", "-q", "show"]:
                return CommandResult(1, "", "failed")
            return super().run(args, input_text=input_text)

    OpenWrtUciAdapter(_NoShow()).reconcile(parse_config_body({"configVersionInc": 1, **_body(_ssid(1))}), _caps())

    assert "Rate Control the agent set before is not removed" in caplog.text
