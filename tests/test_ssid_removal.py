from open_omada_device_agent.adapters.inbound.ecsp.mappers.configuration import parse_config_body
from open_omada_device_agent.adapters.outbound.openwrt.uci import build_uci_batch, parse_uci_show
from open_omada_device_agent.application.contracts import PlatformCapabilities
from open_omada_device_agent.contexts.wireless.domain import RadioBand

# Two agent SSIDs per band, plus one SSID the user made by hand.
STATE = parse_uci_show("""wireless.openomada_2g_1=wifi-iface
wireless.openomada_2g_1.device='radio0'
wireless.openomada_2g_1.openomada_managed='1'
wireless.openomada_2g_2=wifi-iface
wireless.openomada_2g_2.device='radio0'
wireless.openomada_2g_2.openomada_managed='1'
wireless.openomada_5g_1=wifi-iface
wireless.openomada_5g_1.device='radio1'
wireless.openomada_5g_1.openomada_managed='1'
wireless.openomada_5g_2=wifi-iface
wireless.openomada_5g_2.device='radio1'
wireless.openomada_5g_2.openomada_managed='1'
wireless.my_own=wifi-iface
wireless.my_own.device='radio0'
""")

CAPS = PlatformCapabilities(
    platform="openwrt",
    has_uci=True,
    radio_bands=(RadioBand.TWO_G, RadioBand.FIVE_G),
    max_ssids=4,
    supports_wlan_config=True,
    supports_wpa2_psk=True,
)


def _ssid(index):
    return {"ssidName": "lab", "index": index, "securityMode": 0}


def test_ssid_removed_in_the_controller_is_deleted_on_that_band_only():
    update = parse_config_body({"ssid_2G": {"radioId": 0, "ssid": [_ssid(1)]}})

    lines = build_uci_batch(update, CAPS, wireless_state=STATE)

    assert "delete wireless.openomada_2g_2" in lines
    # The kept SSID is rebuilt as usual: deleted once and set again.
    assert lines.count("delete wireless.openomada_2g_1") == 1
    assert "set wireless.openomada_2g_1=wifi-iface" in lines
    assert not any("openomada_5g" in line for line in lines)  # 5 GHz list not sent
    assert not any("my_own" in line for line in lines)  # not created by the agent


def test_empty_list_removes_every_agent_ssid_on_the_band():
    update = parse_config_body({"ssid_5G": {"radioId": 1, "ssid": []}})

    lines = build_uci_batch(update, CAPS, wireless_state=STATE)

    assert "delete wireless.openomada_5g_1" in lines
    assert "delete wireless.openomada_5g_2" in lines
    assert update.wlan_bands == (RadioBand.FIVE_G,)


def test_set_without_ssid_keys_removes_nothing():
    update = parse_config_body({"led": {"enable": 1}})
    assert update.wlan_bands == ()
    assert not any("delete wireless.openomada" in line for line in build_uci_batch(update, CAPS, wireless_state=STATE))
