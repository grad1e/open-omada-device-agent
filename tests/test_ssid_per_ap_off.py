"""An SSID switched off for one AP (Devices > AP > Config > WLANs > Enable).

Observed (lab, Controller 6.3.0.111): the SSID item comes with operation 2
instead of 1, and a full sync leaves the band's ssid key out when no SSID is
left on it for the AP.
"""
from open_omada_device_agent.ap_config import parse_config_body
from open_omada_device_agent.domain import RadioBand
from open_omada_device_agent.openwrt import build_uci_batch, parse_uci_show
from open_omada_device_agent.platform_capabilities import PlatformCapabilities

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


def _item(index, operation, name="lab"):
    return {"index": index, "id": 100 + index, "ssidName": name, "operation": operation}


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
