from open_omada_device_agent.adapters.inbound.ecsp.mappers.configuration import (
    parse_config_body,
)
from open_omada_device_agent.adapters.outbound.openwrt.uci import (
    OpenWrtUciAdapter,
    _wlan_section,
    build_uci_batch,
    parse_uci_show,
    validate_update,
)
from open_omada_device_agent.application.contracts import PlatformCapabilities
from open_omada_device_agent.capabilities import ap_components_v2
from open_omada_device_agent.contexts.wireless.domain import ClientLimit, RadioBand
from open_omada_device_agent.openwrt import CommandResult

UCI_SHOW = """wireless.radio0=wifi-device
wireless.radio0.band='2g'
wireless.radio0.maxassoc='30'
wireless.radio1=wifi-device
wireless.radio1.band='5g'
wireless.radio1.openomada_uapsd='0'
wireless.radio1.rssi_reject_assoc_rssi='-75'
wireless.openomada_2g_1=wifi-iface
wireless.openomada_2g_1.device='radio0'
wireless.openomada_2g_1.openomada_managed='1'
wireless.default_radio0=wifi-iface
wireless.default_radio0.device='radio0'
"""


def _caps(**overrides):
    values = {
        "platform": "openwrt",
        "has_uci": True,
        "radio_bands": (RadioBand.TWO_G, RadioBand.FIVE_G),
        "max_ssids": 4,
        "supports_wlan_config": True,
        "supports_wpa2_psk": True,
    }
    values.update(overrides)
    return PlatformCapabilities(**values)


def test_parse_uci_show_keeps_types_and_options():
    state = parse_uci_show(UCI_SHOW)

    assert state["radio1"][".type"] == "wifi-device"
    assert state["radio1"]["rssi_reject_assoc_rssi"] == "-75"
    assert state["openomada_2g_1"]["device"] == "radio0"


def test_mapper_merges_advanced_keys_per_band():
    update = parse_config_body(
        {
            "loadBalance_2G": {"radioId": 0, "enable": 1, "maxAssocClient": 20},
            "rssi_2G": {"enable": False, "threshold": -95},
            "qosConfig_2G": {"radioId": 0, "uapsdEnable": 0, "wmmEnable": 1},
            "wirelessAdv_5G": {"radioId": 1, "ofdma": 1, "dtimPeriod": 1},
            "rssi_5G": {"enable": True, "threshold": -80},
        }
    )

    assert update.unhandled_keys == ()
    by_band = {advanced.band: advanced for advanced in update.radio_advanced}
    two_g = by_band[RadioBand.TWO_G]
    assert two_g.radio_id == 0
    assert two_g.max_clients == ClientLimit(enabled=True, value=20)
    assert two_g.rssi_threshold == ClientLimit(enabled=False, value=-95)
    assert two_g.uapsd is False
    five_g = by_band[RadioBand.FIVE_G]
    assert five_g.rssi_threshold == ClientLimit(enabled=True, value=-80)
    assert five_g.ofdma is True
    assert five_g.max_clients is None and five_g.uapsd is None


def test_batch_sets_radio_limits_and_uapsd_on_managed_ssids():
    update = parse_config_body(
        {
            "loadBalance_2G": {"radioId": 0, "enable": 1, "maxAssocClient": 20},
            "qosConfig_2G": {"radioId": 0, "uapsdEnable": 0},
            "rssi_5G": {"enable": 1, "threshold": -80},
        }
    )

    batch = build_uci_batch(update, _caps(), wireless_state=parse_uci_show(UCI_SHOW))

    assert "set wireless.radio0.maxassoc='20'" in batch
    assert "set wireless.radio0.openomada_uapsd='0'" in batch
    assert "set wireless.openomada_2g_1.uapsd='0'" in batch
    # Unmanaged SSIDs (OpenWrt defaults) are left alone.
    assert not any("default_radio0.uapsd" in line for line in batch)
    assert "set wireless.radio1.rssi_reject_assoc_rssi='-80'" in batch
    assert batch[-1] == "commit wireless"


def test_batch_removes_limits_only_when_they_are_set():
    update = parse_config_body(
        {
            "loadBalance_2G": {"radioId": 0, "enable": 0, "maxAssocClient": 1},
            "loadBalance_5G": {"radioId": 1, "enable": 0, "maxAssocClient": 1},
            "rssi_5G": {"enable": 0, "threshold": -95},
        }
    )

    batch = build_uci_batch(update, _caps(), wireless_state=parse_uci_show(UCI_SHOW))

    assert "delete wireless.radio0.maxassoc" in batch
    assert "delete wireless.radio1.rssi_reject_assoc_rssi" in batch
    assert not any("radio1.maxassoc" in line for line in batch)


def test_rebuilt_ssids_get_the_radio_uapsd_setting_again():
    update = parse_config_body(
        {"ssid_5G": {"ssid": [{"ssidName": "lab", "ssidId": 7}]}}
    )
    section = _wlan_section(update.wlans[0])

    batch = build_uci_batch(update, _caps(), wireless_state=parse_uci_show(UCI_SHOW))

    assert f"set wireless.{section}.uapsd='0'" in batch
    assert batch.index(f"set wireless.{section}.uapsd='0'") > batch.index(
        f"delete wireless.{section}"
    )


def test_ofdma_alone_changes_nothing():
    update = parse_config_body({"wirelessAdv_2G": {"radioId": 0, "ofdma": 0}})

    assert build_uci_batch(update, _caps(), wireless_state={}) == ()


def test_validation_rejects_bad_limits():
    update = parse_config_body(
        {
            "loadBalance_2G": {"enable": 1, "maxAssocClient": 0},
            "rssi_5G": {"enable": 1, "threshold": 10},
        }
    )

    errors = validate_update(update, _caps())

    assert any("Maximum Associated Clients" in error for error in errors)
    assert any("RSSI Threshold" in error for error in errors)
    assert validate_update(update, _caps(supports_wlan_config=False))


class _Runner:
    def __init__(self):
        self.calls = []

    def run(self, args, *, input_text=None):
        self.calls.append((tuple(args), input_text))
        if tuple(args) == ("uci", "-q", "show", "wireless"):
            return CommandResult(0, UCI_SHOW)
        return CommandResult(0)


def test_adapter_reads_state_then_applies_and_reloads():
    runner = _Runner()
    update = parse_config_body({"qosConfig_5G": {"radioId": 1, "uapsdEnable": 1}})

    result = OpenWrtUciAdapter(runner).reconcile(update, _caps())

    assert (result.applied, result.changed) == (True, True)
    assert [call[0] for call in runner.calls] == [
        ("uci", "-q", "show", "wireless"),
        ("uci", "-q", "batch"),
        ("wifi", "reload"),
    ]
    assert "set wireless.radio1.openomada_uapsd='1'" in runner.calls[1][1]


def test_components_follow_wlan_support():
    assert "rssiFilter" not in ap_components_v2(_caps(supports_wlan_config=False))
    components = ap_components_v2(_caps())
    assert components["lb"] == "1.0"
    assert components["rssiFilter"] == "1.0"
    assert components["qos"] == "1.0"
    assert components["wlanAdv"] == "1.1"
