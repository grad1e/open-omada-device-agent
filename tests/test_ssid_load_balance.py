"""SSID "Load Balance" -> 802.11k/v on OpenWrt."""
from open_omada_device_agent.adapters.outbound.openwrt.capabilities import (
    detect_platform_capabilities,
)
from open_omada_device_agent.ap_config import parse_config_body
from open_omada_device_agent.domain import RadioBand
from open_omada_device_agent.openwrt import build_uci_batch, validate_update
from open_omada_device_agent.platform_capabilities import PlatformCapabilities


def _caps(**overrides):
    values = {
        "platform": "openwrt",
        "radio_bands": (RadioBand.TWO_G, RadioBand.FIVE_G),
        "max_ssids": 4,
        "supports_wlan_config": True,
        "supports_wpa2_psk": True,
        "supports_80211k": True,
        "supports_80211v": True,
    }
    values.update(overrides)
    return PlatformCapabilities(**values)


def _update(**ssid):
    item = {"index": 1, "ssidName": "lab", "pskKey": "network-passphrase", "pskVer": 2, **ssid}
    return parse_config_body({"ssid_2G": {"radioId": 0, "ssid": [item]}})


def _options(batch):
    return {
        line.split(" ", 1)[1].split("=", 1)[0].rsplit(".", 1)[1]: line.split("=", 1)[1].strip("'")
        for line in batch
        if line.startswith("set ") and ".openomada_2g_1." in line
    }


def test_load_balance_on_enables_80211k_and_bss_transition():
    options = _options(build_uci_batch(_update(loadBalance=1), _caps()))

    assert options["ieee80211k"] == "1"
    assert options["bss_transition"] == "1"


def test_load_balance_without_80211v_writes_only_80211k():
    # wpad-basic refuses bss_transition and leaves the radio down (Observed).
    options = _options(build_uci_batch(_update(loadBalance=1), _caps(supports_80211v=False)))

    assert options["ieee80211k"] == "1"
    assert "bss_transition" not in options


def test_load_balance_on_an_open_ssid():
    update = parse_config_body(
        {"ssid_2G": {"radioId": 0, "ssid": [{"index": 1, "ssidName": "lab", "loadBalance": 1}]}}
    )

    options = _options(build_uci_batch(update, _caps()))

    assert options["encryption"] == "none"
    assert options["ieee80211k"] == "1"


def test_load_balance_off_or_missing_writes_nothing():
    for update in (_update(loadBalance=0), _update()):
        options = _options(build_uci_batch(update, _caps()))
        assert "ieee80211k" not in options
        assert "bss_transition" not in options


def test_load_balance_needs_the_capability():
    errors = validate_update(_update(loadBalance=1), _caps(supports_80211k=False))

    assert any("Load Balance requested" in message for message in errors)


def test_load_balance_off_is_accepted_without_the_capability():
    assert validate_update(_update(loadBalance=0), _caps(supports_80211k=False)) == ()


def _detect(succeeding, env=None):
    tools = {"uci", "ubus", "hostapd"}
    return detect_platform_capabilities(
        env={"OMADA_PLATFORM": "openwrt", **(env or {})},
        command_exists=lambda name: f"/usr/sbin/{name}" if name in tools else None,
        command_output=lambda args: None,
        command_succeeds=lambda args: args in succeeding,
    )


def test_80211k_and_80211v_follow_the_hostapd_build():
    wnm = {("grep", "-q", "wnm_sleep_mode", "/usr/sbin/hostapd")}

    full = _detect(wnm)
    basic = _detect(set())

    assert (full.supports_80211k, full.supports_80211v) == (True, True)
    assert (basic.supports_80211k, basic.supports_80211v) == (True, False)


def test_80211k_and_80211v_can_be_switched_off():
    caps = _detect(
        {("grep", "-q", "wnm_sleep_mode", "/usr/sbin/hostapd")},
        env={"OMADA_CAP_80211K": "false", "OMADA_CAP_80211V": "false"},
    )

    assert (caps.supports_80211k, caps.supports_80211v) == (False, False)
