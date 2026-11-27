"""SSID Security (securityMode): only None and WPA-Personal are applied.

Anything else gets no key from OpenWrt's point of view, so writing it would
create an open network; those SSIDs are refused instead.
"""
import pytest

from open_omada_device_agent.ap_config import parse_config_body
from open_omada_device_agent.domain import RadioBand
from open_omada_device_agent.openwrt import build_uci_batch, validate_update
from open_omada_device_agent.platform_capabilities import PlatformCapabilities

KEY = "network-passphrase"  # made up


def _caps(**overrides):
    values = {
        "platform": "openwrt",
        "radio_bands": (RadioBand.TWO_G, RadioBand.FIVE_G),
        "max_ssids": 4,
        "supports_wlan_config": True,
        "supports_wpa2_psk": True,
        "supports_radius": True,
    }
    values.update(overrides)
    return PlatformCapabilities(**values)


def _update(**ssid):
    item = {"index": 1, "ssidName": "lab", **ssid}
    return parse_config_body({"ssid_2G": {"radioId": 0, "ssid": [item]}})


def _encryption(batch):
    return next(line for line in batch if ".openomada_2g_1.encryption=" in line).split("=", 1)[1]


def test_wpa_personal_with_a_key_is_applied():
    update = _update(securityMode=3, pskVer=2, pskCipher=3, pskKey=KEY)

    assert validate_update(update, _caps()) == ()
    assert _encryption(build_uci_batch(update, _caps())) == "'psk2+ccmp'"


@pytest.mark.parametrize("ssid", [{}, {"pskKey": ""}, {"pskKey": None}])
def test_wpa_personal_without_a_key_is_refused(ssid):
    errors = validate_update(_update(securityMode=3, pskVer=2, **ssid), _caps())

    assert any("WPA-Personal without a Wi-Fi key" in error for error in errors)


def test_none_is_open_even_with_a_leftover_key():
    # Switching an SSID from WPA-Personal to None may still carry the old key.
    update = _update(securityMode=0, pskKey=KEY)

    assert validate_update(update, _caps()) == ()
    batch = build_uci_batch(update, _caps())
    assert _encryption(batch) == "'none'"
    assert not any(".openomada_2g_1.key=" in line for line in batch)


@pytest.mark.parametrize(
    ("ssid", "message"),
    [
        ({"securityMode": 2, "radiusAuth": {"servers": []}}, "WPA-Enterprise is not supported"),
        ({"securityMode": 4}, "PPSK is not supported"),
        ({"securityMode": 5}, "PPSK is not supported"),
        ({"securityMode": 0, "oweEnable": True}, "OWE (Enhanced Open) is not supported"),
        ({"securityMode": 9}, "unsupported security mode code 9"),
    ],
)
def test_other_security_modes_are_refused_not_opened(ssid, message):
    errors = validate_update(_update(**ssid), _caps())

    assert any(message in error for error in errors)


def test_without_a_security_mode_the_key_decides():
    assert _encryption(build_uci_batch(_update(pskKey=KEY, pskVer=2, pskCipher=3), _caps())) == "'psk2+ccmp'"
    assert _encryption(build_uci_batch(_update(), _caps())) == "'none'"
