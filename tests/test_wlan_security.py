"""WPA-Personal version/cipher, PMF and 802.11r from the SSID editor."""
import pytest

from open_omada_device_agent.adapters.outbound.openwrt.capabilities import (
    detect_platform_capabilities,
)
from open_omada_device_agent.ap_config import parse_config_body
from open_omada_device_agent.domain import RadioBand
from open_omada_device_agent.openwrt import build_uci_batch, validate_update
from open_omada_device_agent.platform_capabilities import PlatformCapabilities

FT_KEY = "00112233445566778899aabbccddeeff"  # made up


def _caps(**overrides):
    values = {
        "platform": "openwrt",
        "radio_bands": (RadioBand.TWO_G, RadioBand.FIVE_G),
        "max_ssids": 4,
        "supports_wlan_config": True,
        "supports_wpa2_psk": True,
        "supports_wpa3_psk": True,
        "supports_80211r": True,
    }
    values.update(overrides)
    return PlatformCapabilities(**values)


def _update(**ssid):
    item = {"index": 1, "ssidName": "lab", "pskKey": "network-passphrase", **ssid}
    return parse_config_body({"ssid_2G": {"radioId": 0, "ssid": [item]}})


def _options(batch):
    """option -> value for the SSID section, list options as tuples."""
    options = {}
    for line in batch:
        if ".openomada_2g_1." not in line:
            continue
        verb, rest = line.split(" ", 1)
        name, value = rest.split("=", 1)
        option = name.rsplit(".", 1)[1]
        value = value.strip("'")
        if verb == "add_list":
            options[option] = (*options.get(option, ()), value)
        else:
            options[option] = value
    return options


@pytest.mark.parametrize(
    ("version", "cipher", "encryption"),
    [
        (1, 1, "psk+tkip+ccmp"),  # WPA-PSK / Auto
        (1, 3, "psk+ccmp"),
        (2, 1, "psk2+tkip+ccmp"),  # WPA2-PSK / Auto
        (2, 3, "psk2+ccmp"),
        (3, 1, "psk-mixed+tkip+ccmp"),  # WPA/WPA2-PSK
        (3, 3, "psk-mixed+ccmp"),
        (4, 3, "sae-mixed"),  # WPA2-PSK/WPA3-SAE, AES only
        (2, None, "psk2"),
        (None, None, "psk2"),  # no version from the controller
    ],
)
def test_wpa_mode_maps_to_openwrt_encryption(version, cipher, encryption):
    ssid = {"pskVer": version, "pskCipher": cipher}
    batch = build_uci_batch(_update(**{k: v for k, v in ssid.items() if v is not None}), _caps())

    assert _options(batch)["encryption"] == encryption


def test_wpa_wpa2_mixed_is_not_treated_as_wpa3():
    # pskVer 3 used to be written as sae-mixed and refused without WPA3.
    update = _update(pskVer=3, pskCipher=3)

    assert validate_update(update, _caps(supports_wpa3_psk=False)) == ()


def test_wpa3_needs_the_wpa3_capability():
    errors = validate_update(_update(pskVer=4, pskCipher=3), _caps(supports_wpa3_psk=False))

    assert errors == ("WPA3-PSK WLAN requested but WPA3-PSK capability is disabled",)


@pytest.mark.parametrize(
    ("ssid", "error"),
    [
        ({"pskVer": 9}, "unsupported WPA-Personal version code 9"),
        ({"pskVer": 2, "pskCipher": 7}, "unsupported WPA cipher code 7"),
        ({"pskVer": 2, "pmfMode": 5}, "unsupported PMF mode code 5"),
    ],
)
def test_unknown_security_codes_are_refused(ssid, error):
    assert error in validate_update(_update(**ssid), _caps())


@pytest.mark.parametrize(
    ("version", "pmf", "expected"),
    [
        (2, 1, "2"),  # Mandatory
        (2, 2, "1"),  # Capable
        (2, 3, "0"),  # Disable
        (4, 1, "2"),
        (4, 2, "1"),
        (4, 3, None),  # WPA3 needs PMF; leave OpenWrt's default
        (1, 2, None),  # WPA (v1) has no PMF
        (2, None, None),
    ],
)
def test_pmf_mode_maps_to_ieee80211w(version, pmf, expected):
    ssid = {"pskVer": version, "pskCipher": 3}
    if pmf is not None:
        ssid["pmfMode"] = pmf
    options = _options(build_uci_batch(_update(**ssid), _caps()))

    assert options.get("ieee80211w") == expected


def test_80211r_writes_mobility_domain_and_key_holders():
    update = _update(
        pskVer=4,
        pskCipher=3,
        fastTransition={"enable11r": True, "mobilityDomain": "A4CB", "keyRrb": FT_KEY.upper()},
    )

    options = _options(build_uci_batch(update, _caps()))

    assert options["ieee80211r"] == "1"
    assert options["mobility_domain"] == "a4cb"
    assert options["ft_over_ds"] == "0"
    assert options["ft_psk_generate_local"] == "1"
    assert options["r0kh"] == (f"ff:ff:ff:ff:ff:ff,*,{FT_KEY}",)
    assert options["r1kh"] == (f"00:00:00:00:00:00,00:00:00:00:00:00,{FT_KEY}",)


def test_80211r_off_or_on_wpa_v1_writes_nothing():
    off = _update(pskVer=2, fastTransition={"enable11r": False, "mobilityDomain": "A4CB"})
    wpa1 = _update(pskVer=1, fastTransition={"enable11r": True, "mobilityDomain": "A4CB"})

    assert "ieee80211r" not in _options(build_uci_batch(off, _caps()))
    assert "ieee80211r" not in _options(build_uci_batch(wpa1, _caps()))


@pytest.mark.parametrize(
    ("fast_transition", "caps", "error"),
    [
        ({"enable11r": True, "mobilityDomain": "A4CB"}, {"supports_80211r": False}, "802.11r requested"),
        ({"enable11r": True, "mobilityDomain": "XYZ1"}, {}, "4-digit hex mobility domain"),
        ({"enable11r": True}, {}, "4-digit hex mobility domain"),
        ({"enable11r": True, "mobilityDomain": "A4CB", "keyRrb": "abc"}, {}, "not 32 or 64 hex digits"),
    ],
)
def test_80211r_checks(fast_transition, caps, error):
    errors = validate_update(_update(pskVer=2, fastTransition=fast_transition), _caps(**caps))

    assert any(error in message for message in errors)


def test_mapper_keeps_the_80211r_key_secret():
    update = _update(fastTransition={"enable11r": True, "mobilityDomain": "A4CB", "keyRrb": FT_KEY})
    security = update.wlans[0].security

    assert security.ft_mobility_domain == "A4CB"
    assert security.ft_key.reveal() == FT_KEY
    assert FT_KEY not in repr(security)
    assert "network-passphrase" not in repr(update.wlans[0])


def _detect(succeeding, env=None):
    tools = {"uci", "ubus", "hostapd"}
    return detect_platform_capabilities(
        env={"OMADA_PLATFORM": "openwrt", **(env or {})},
        command_exists=lambda name: f"/usr/bin/{name}" if name in tools else None,
        command_output=lambda args: None,
        command_succeeds=lambda args: args in succeeding,
    )


def test_wpa3_and_80211r_follow_the_hostapd_build():
    full = _detect({("hostapd", "-vsae"), ("hostapd", "-v11r")})
    bare = _detect(set())

    assert (full.supports_wpa3_psk, full.supports_80211r) == (True, True)
    assert (bare.supports_wpa3_psk, bare.supports_80211r) == (False, False)


def test_wpa3_and_80211r_can_be_switched_off():
    caps = _detect(
        {("hostapd", "-vsae"), ("hostapd", "-v11r")},
        env={"OMADA_CAP_WPA3_PSK": "false", "OMADA_CAP_80211R": "false"},
    )

    assert (caps.supports_wpa3_psk, caps.supports_80211r) == (False, False)
