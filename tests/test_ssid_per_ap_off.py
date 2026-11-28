"""An SSID switched off for one AP (Devices > AP > Config > WLANs > Enable).

Observed (lab, Controller 6.3.0.111): the SSID item comes with operation 2
instead of 1, and a full sync leaves the band's ssid key out when no SSID is
left on it for the AP.
"""
from open_omada_device_agent.ap_config import parse_config_body
from open_omada_device_agent.domain import RadioBand
from open_omada_device_agent.adapters.outbound.openwrt.uci import (
    _missing_radio_errors,
    radio_sections_by_band,
    validate_update,
)
from open_omada_device_agent.openwrt import build_uci_batch, parse_uci_show
from open_omada_device_agent.platform_capabilities import PlatformCapabilities
from open_omada_device_agent.portal_runtime import OpenWrtPortalRuntime

CAPS = PlatformCapabilities(
    platform="openwrt",
    radio_bands=(RadioBand.TWO_G, RadioBand.FIVE_G),
    max_ssids=4,
    supports_wlan_config=True,
    supports_wpa2_psk=True,
)

SHOW = parse_uci_show(
    "wireless.radio0=wifi-device\n"
    "wireless.radio0.band='2g'\n"
    "wireless.radio1=wifi-device\n"
    "wireless.radio1.band='5g'\n"
    "wireless.openomada_2g_1=wifi-iface\n"
    "wireless.openomada_2g_1.openomada_managed='1'\n"
    "wireless.openomada_5g_1=wifi-iface\n"
    "wireless.openomada_5g_1.openomada_managed='1'\n"
    "wireless.default_radio0=wifi-iface\n"
)


def _item(index, operation, name="lab", **extra):
    return {"index": index, "id": 100 + index, "ssidName": name, "operation": operation, **extra}


def test_ssid_switched_off_for_this_ap_is_left_out():
    update = parse_config_body({"ssid_2G": {"radioId": 0, "ssid": [_item(1, 2), _item(2, 1, "guest")]}})

    assert [wlan.index for wlan in update.wlans] == [2]
    assert update.wlan_bands == (RadioBand.TWO_G,)


def test_ssid_switched_off_for_this_ap_is_removed():
    update = parse_config_body({"ssid_2G": {"radioId": 0, "ssid": [_item(1, 2)]}})
    batch = build_uci_batch(update, CAPS, wireless_state=SHOW)

    assert "delete wireless.openomada_2g_1" in batch
    assert not any(line.startswith("set wireless.openomada_2g_1") for line in batch)
    # Other bands and sections the agent did not create are kept.
    assert not any("openomada_5g_1" in line or "default_radio0" in line for line in batch)


def test_ssid_switched_back_on_is_built():
    update = parse_config_body({"ssid_2G": {"radioId": 0, "ssid": [_item(1, 1)]}})
    batch = build_uci_batch(update, CAPS, wireless_state=SHOW)

    assert "set wireless.openomada_2g_1=wifi-iface" in batch


def test_full_sync_without_ssid_keys_removes_the_agent_ssids():
    update = parse_config_body({"configVersion": 7, "led": {"enable": True}})
    batch = build_uci_batch(update, CAPS, wireless_state=SHOW)

    assert "delete wireless.openomada_2g_1" in batch
    assert "delete wireless.openomada_5g_1" in batch
    assert not any("default_radio0" in line for line in batch)


def test_incremental_set_without_ssid_keys_keeps_the_ssids():
    update = parse_config_body({"led": {"enable": True}})

    assert update.wlan_bands == ()
    assert not any("delete wireless.openomada_" in line for line in build_uci_batch(update, CAPS, wireless_state=SHOW))


def test_full_sync_lists_bands_without_a_radio_but_writes_nothing_there():
    # A full sync lists every band, including 5 GHz-2 and 6 GHz, which this AP lacks.
    update = parse_config_body({"configVersion": 7, "ssid_2G": {"radioId": 0, "ssid": [_item(1, 1)]}})
    radios = radio_sections_by_band(SHOW)

    assert RadioBand.FIVE_G2 in update.wlan_bands
    assert RadioBand.SIX_G in update.wlan_bands
    assert _missing_radio_errors(update, CAPS, radios) == []
    assert validate_update(update, CAPS) == ()
    batch = build_uci_batch(update, CAPS, wireless_state=SHOW)
    assert not any(token in line for line in batch for token in ("5g2", "6g", "radio2", "radio3"))


def test_ssid_switched_off_does_not_count_toward_the_ssid_limit():
    caps = PlatformCapabilities(
        platform="openwrt",
        radio_bands=(RadioBand.TWO_G, RadioBand.FIVE_G),
        max_ssids=1,
        supports_wlan_config=True,
        supports_wpa2_psk=True,
    )
    update = parse_config_body({"ssid_2G": {"radioId": 0, "ssid": [_item(1, 2), _item(2, 1, "guest")]}})
    batch = build_uci_batch(update, caps, wireless_state=SHOW)

    assert "set wireless.openomada_2g_2=wifi-iface" in batch
    assert "delete wireless.openomada_2g_1" in batch


def test_portal_ssid_switched_off_does_not_need_the_portal():
    # CAPS has no portal support: a portal SSID that is on is refused.
    on = parse_config_body({"ssid_2G": {"radioId": 0, "ssid": [_item(1, 1, portal=True)]}})
    off = parse_config_body({"ssid_2G": {"radioId": 0, "ssid": [_item(1, 2, portal=True)]}})
    runtime = OpenWrtPortalRuntime(interface="br-lan", redirect_port=2050)

    assert validate_update(on, CAPS) != ()
    assert not runtime.reconcile(on, CAPS).applied
    assert validate_update(off, CAPS) == ()
    result = runtime.reconcile(off, CAPS)
    assert result.applied and not result.changed
