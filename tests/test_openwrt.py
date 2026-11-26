from dataclasses import dataclass, field

import pytest

from open_omada_device_agent.ap_config import parse_config_body
from open_omada_device_agent.domain import RadioBand
import open_omada_device_agent.openwrt as openwrt
from open_omada_device_agent.openwrt import CommandResult, OpenWrtUciAdapter, build_uci_batch
from open_omada_device_agent.platform_capabilities import PlatformCapabilities


@dataclass
class RecordingRunner:
    calls: list[tuple[tuple[str, ...], str | None]] = field(default_factory=list)
    fail_wifi_reload: bool = False

    def run(self, args, *, input_text=None):
        self.calls.append((tuple(args), input_text))
        if self.fail_wifi_reload and tuple(args) == ("wifi", "reload"):
            return CommandResult(returncode=1, stderr="reload failed")
        return CommandResult(returncode=0)


def _caps(**overrides):
    values = {
        "platform": "openwrt",
        "radio_bands": (RadioBand.TWO_G, RadioBand.FIVE_G),
        "max_ssids": 4,
        "supports_wlan_config": True,
        "supports_wpa2_psk": True,
    }
    values.update(overrides)
    return PlatformCapabilities(**values)


def test_builds_idempotent_uci_batch_for_radio_and_psk_wlan():
    update = parse_config_body(
        {
            "wirelessBasic_2G": {
                "radioId": 0,
                "radioEnable": True,
                "channel": 11,
                "chanWidth": 2,
                "txPower": 14,
            },
            "ssid_2G": {
                "radioId": 0,
                "ssid": [
                    {
                        "index": 1,
                        "ssidName": "Open Omada",
                        "ssidBcast": True,
                        "ssidIsolation": False,
                        "pskVer": 2,
                        "pskKey": "network-passphrase",
                    }
                ],
            },
        }
    )

    batch = build_uci_batch(update, _caps())

    assert "set wireless.radio0.disabled='0'" in batch
    assert "set wireless.radio0.channel='11'" in batch
    assert "set wireless.radio0.htmode='HT20'" in batch
    assert "set wireless.default_radio0.disabled='1'" in batch
    assert "delete wireless.openomada_2g_1" in batch
    assert "set wireless.openomada_2g_1=wifi-iface" in batch
    assert "set wireless.openomada_2g_1.ssid='Open Omada'" in batch
    assert "set wireless.openomada_2g_1.encryption='psk2'" in batch
    assert "set wireless.openomada_2g_1.key='network-passphrase'" in batch
    assert "openomada_ssid_id" not in batch  # no controller id in this push
    assert batch[-1] == "commit wireless"


@pytest.mark.parametrize(
    ("band_key", "radio_id", "code", "current", "expected"),
    [
        ("wirelessBasic_2G", 0, 2, None, "HT20"),
        ("wirelessBasic_2G", 0, 3, None, "HT40"),
        ("wirelessBasic_5G", 1, 3, None, "VHT40"),
        ("wirelessBasic_5G", 1, 5, None, "VHT80"),
        ("wirelessBasic_5G", 1, 5, "HE20", "HE80"),
        ("wirelessBasic_5G", 1, 7, "HE80", "HE160"),
        ("wirelessBasic_2G", 0, 4, "HE20", "HE40"),
        ("wirelessBasic_5G", 1, 6, "HE40", "HE80"),
        ("wirelessBasic_2G", 0, 2, "HE40", "HE20"),
        ("wirelessBasic_2G", 0, 3, "VHT20", "HT40"),
        ("wirelessBasic_5G", 1, 5, "HT20", "VHT80"),
    ],
)
def test_maps_omada_chan_width_code_to_htmode_keeping_family(
    band_key, radio_id, code, current, expected
):
    update = parse_config_body({band_key: {"radioId": radio_id, "chanWidth": code}})
    section = f"radio{radio_id}"

    batch = build_uci_batch(
        update, _caps(), current_htmodes={section: current} if current else None
    )

    assert f"set wireless.{section}.htmode='{expected}'" in batch


def test_auto_channel_limits_acs_to_controller_channel_range():
    update = parse_config_body(
        {
            "wirelessBasic_5G": {
                "radioId": 1,
                "channel": 0,
                "channelRange": [5180, 5200, 5220, 5240, 5745, 5765, 5785, 5805, 5825],
            }
        }
    )

    batch = build_uci_batch(update, _caps())

    assert "set wireless.radio1.channel='auto'" in batch
    assert "set wireless.radio1.channels='36 40 44 48 149 153 157 161 165'" in batch
    assert "set wireless.radio1.channel='0'" not in batch


def test_auto_channel_maps_2g_frequencies_and_keeps_list_without_range():
    with_range = parse_config_body(
        {"wirelessBasic_2G": {"radioId": 0, "channel": 0, "channelRange": [2412, 2437, 2462]}}
    )
    without_range = parse_config_body({"wirelessBasic_2G": {"radioId": 0, "channel": 0}})

    assert "set wireless.radio0.channels='1 6 11'" in build_uci_batch(with_range, _caps())
    batch = build_uci_batch(without_range, _caps())
    assert "set wireless.radio0.channel='auto'" in batch
    assert not any("radio0.channels" in line for line in batch)


def test_auto_channel_without_range_falls_back_to_advertised_non_dfs_channels():
    from open_omada_device_agent.contexts.wireless.domain import RadioChannel, RadioHardware

    hardware = RadioHardware(
        band=RadioBand.FIVE_G,
        channels=(
            RadioChannel(36, 5180),
            RadioChannel(52, 5260, radar=True),
            RadioChannel(149, 5745),
        ),
    )
    update = parse_config_body({"wirelessBasic_5G": {"radioId": 1, "channel": 0}})

    batch = build_uci_batch(update, _caps(radio_hardware=(hardware,)))

    assert "set wireless.radio1.channels='36 149'" in batch


def test_auto_channel_without_range_includes_radar_channels_when_dfs_works():
    from dataclasses import replace

    from open_omada_device_agent.contexts.wireless.domain import RadioChannel, RadioHardware

    hardware = RadioHardware(
        band=RadioBand.FIVE_G,
        channels=(
            RadioChannel(36, 5180),
            RadioChannel(52, 5260, radar=True),
            RadioChannel(149, 5745),
        ),
    )
    update = parse_config_body({"wirelessBasic_5G": {"radioId": 1, "channel": 0}})

    batch = build_uci_batch(
        update, _caps(radio_hardware=(replace(hardware, dfs=True),))
    )

    assert "set wireless.radio1.channels='36 52 149'" in batch


def test_fixed_channel_does_not_touch_acs_channel_list():
    update = parse_config_body(
        {"wirelessBasic_5G": {"radioId": 1, "channel": 149, "channelRange": [5180, 5745]}}
    )

    batch = build_uci_batch(update, _caps())

    assert "set wireless.radio1.channel='149'" in batch
    assert not any("radio1.channels" in line for line in batch)


def test_unknown_chan_width_code_leaves_htmode_unchanged(caplog):
    update = parse_config_body({"wirelessBasic_5G": {"radioId": 1, "chanWidth": 99}})
    caplog.set_level("WARNING")

    batch = build_uci_batch(update, _caps())

    assert not any(".htmode=" in line for line in batch)
    assert "Unknown Omada chanWidth code 99" in caplog.text


@dataclass
class HtmodeRunner(RecordingRunner):
    htmode: str = "HE80"

    def run(self, args, *, input_text=None):
        if tuple(args[:3]) == ("uci", "-q", "get"):
            self.calls.append((tuple(args), input_text))
            return CommandResult(returncode=0, stdout=f"{self.htmode}\n")
        return super().run(args, input_text=input_text)


def test_reconcile_reads_current_htmode_before_changing_width():
    update = parse_config_body({"wirelessBasic_5G": {"radioId": 1, "chanWidth": 3}})
    runner = HtmodeRunner(htmode="HE80")

    result = OpenWrtUciAdapter(runner).reconcile(update, _caps())

    assert result.applied is True
    assert runner.calls[0][0] == ("uci", "-q", "get", "wireless.radio1.htmode")
    assert "set wireless.radio1.htmode='HE40'" in (runner.calls[1][1] or "")


def test_reconcile_runs_uci_batch_and_wifi_reload_without_shell():
    update = parse_config_body(
        {"ssid_2G": {"radioId": 0, "ssid": [{"ssidName": "lab", "pskKey": "secret"}]}}
    )
    runner = RecordingRunner()

    result = OpenWrtUciAdapter(runner).reconcile(update, _caps())

    assert result.applied is True
    assert result.changed is True
    calls = _without_state_reads(runner.calls)
    assert calls[0][0] == ("uci", "-q", "batch")
    assert calls[0][1] is not None
    assert calls[1] == (("wifi", "reload"), None)


def test_reconcile_applies_first_ssid_and_logs_capacity_warning(caplog):
    update = parse_config_body(
        {
            "ssid_2G": {
                "radioId": 0,
                "ssid": [
                    {"index": 1, "ssidName": "guest"},
                    {"index": 2, "ssidName": "corp", "pskKey": "secret"},
                ],
            }
        }
    )
    runner = RecordingRunner()
    caplog.set_level("WARNING")

    result = OpenWrtUciAdapter(runner).reconcile(update, _caps(max_ssids=1))

    assert result.applied is True
    assert result.changed is True
    assert result.error == ""
    assert "controller requested 2 SSIDs" in caplog.text
    calls = _without_state_reads(runner.calls)
    batch = calls[0][1] or ""
    assert "set wireless.default_radio0.disabled='1'" in batch
    assert "set wireless.openomada_2g_1.ssid='guest'" in batch
    assert "delete wireless.openomada_2g_2" in batch
    assert "set wireless.openomada_2g_2.ssid='corp'" not in batch


def test_reconcile_rejects_vlan_when_capability_is_disabled():
    update = parse_config_body(
        {"ssid_2G": {"radioId": 0, "ssid": [{"ssidName": "lab", "vlanId": 30}]}}
    )
    runner = RecordingRunner()

    result = OpenWrtUciAdapter(runner).reconcile(update, _caps())

    assert result.applied is False
    assert "SSID VLAN requested" in result.error
    assert runner.calls == []


def test_builds_network_interface_for_enabled_ssid_vlan_capability():
    update = parse_config_body(
        {"ssid_2G": {"radioId": 0, "ssid": [{"ssidName": "lab", "vlanId": 30}]}}
    )

    batch = build_uci_batch(update, _caps(supports_ssid_vlan=True))

    assert "delete network.openomada_vlan30" in batch
    assert "set network.openomada_vlan30=interface" in batch
    assert "set network.openomada_vlan30.device='br-lan.30'" in batch
    assert "commit network" in batch
    assert "set wireless.openomada_2g_lab.network='openomada_vlan30'" in batch


def test_builds_management_vlan_device_mapping_when_configured(monkeypatch):
    monkeypatch.setattr(openwrt.config, "MANAGEMENT_VLAN_INTERFACE", "lan")
    monkeypatch.setattr(openwrt.config, "MANAGEMENT_VLAN_DEVICE", "br-lan")
    update = parse_config_body(
        {"managementVlan": {"managementVlanEnable": "on", "managementVlanId": 99}}
    )

    batch = build_uci_batch(update, _caps(supports_management_vlan=True))

    assert "set network.lan.device='br-lan.99'" in batch
    assert "commit network" in batch


def test_reconcile_rejects_enabled_management_vlan_without_target(monkeypatch):
    monkeypatch.setattr(openwrt.config, "MANAGEMENT_VLAN_INTERFACE", "")
    monkeypatch.setattr(openwrt.config, "MANAGEMENT_VLAN_DEVICE", "")
    update = parse_config_body(
        {"managementVlan": {"managementVlanEnable": "on", "managementVlanId": 99}}
    )
    runner = RecordingRunner()

    result = OpenWrtUciAdapter(runner).reconcile(
        update,
        _caps(supports_management_vlan=True),
    )

    assert result.applied is False
    assert "OMADA_MANAGEMENT_VLAN_INTERFACE" in result.error
    assert runner.calls == []


def test_reconcile_accepts_portal_free_policy_as_passive_global_config():
    update = parse_config_body(
        {"portalFreePolicyConfig": {"portalFreePolicy": [{"value": "192.0.2.10"}]}}
    )
    runner = RecordingRunner()

    result = OpenWrtUciAdapter(runner).reconcile(update, _caps(supports_portal=False))

    assert result.applied is True
    assert result.changed is False
    assert runner.calls == []


def test_reconcile_allows_portal_wlan_when_capability_is_enabled():
    update = parse_config_body(
        {"ssid_2G": {"radioId": 0, "ssid": [{"ssidName": "guest", "portal": True}]}}
    )
    runner = RecordingRunner()

    result = OpenWrtUciAdapter(runner).reconcile(update, _caps(supports_portal=True))

    assert result.applied is True
    assert result.changed is True
    calls = _without_state_reads(runner.calls)
    assert calls[0][0] == ("uci", "-q", "batch")
    assert "set wireless.openomada_2g_guest.ssid='guest'" in (calls[0][1] or "")


def test_reconcile_rejects_wpa3_psk_when_capability_is_disabled():
    update = parse_config_body(
        {
            "ssid_2G": {
                "radioId": 0,
                "ssid": [{"ssidName": "lab", "pskVer": 3, "pskKey": "secret"}],
            }
        }
    )
    runner = RecordingRunner()

    result = OpenWrtUciAdapter(runner).reconcile(update, _caps(supports_wpa3_psk=False))

    assert result.applied is False
    assert "WPA3-PSK WLAN requested" in result.error
    assert runner.calls == []


def test_reconcile_reports_wifi_reload_failure():
    update = parse_config_body(
        {"ssid_2G": {"radioId": 0, "ssid": [{"ssidName": "lab", "pskKey": "secret"}]}}
    )
    runner = RecordingRunner(fail_wifi_reload=True)

    result = OpenWrtUciAdapter(runner).reconcile(update, _caps())

    assert result.applied is False
    assert result.changed is True
    assert result.error == "reload failed"


def test_controller_channel_range_drops_channels_the_radio_cannot_use():
    from open_omada_device_agent.contexts.wireless.domain import RadioChannel, RadioHardware

    hardware = RadioHardware(
        band=RadioBand.FIVE_G,
        channels=(
            RadioChannel(36, 5180),
            RadioChannel(100, 5500, radar=True),
            RadioChannel(149, 5745),
        ),
    )
    # Range saved while DFS worked (e.g. another country): 36, 100, 149.
    update = parse_config_body(
        {"wirelessBasic_5G": {"radioId": 1, "channel": 0, "channelRange": [5180, 5500, 5745]}}
    )

    batch = build_uci_batch(update, _caps(radio_hardware=(hardware,)))

    assert "set wireless.radio1.channels='36 149'" in batch


def test_wlan_section_records_the_controller_ssid_id_for_inform_stats():
    update = parse_config_body(
        {
            "ssid_5G": {
                "radioId": 1,
                "ssid": [{"id": 180126366, "index": 1, "ssidName": "corp"}],
            },
        }
    )

    batch = build_uci_batch(update, _caps())

    assert "set wireless.openomada_5g_1.openomada_ssid_id='180126366'" in batch


def _without_state_reads(calls):
    """Drop the ``uci show wireless`` read that precedes SSID/radio batches."""
    return [call for call in calls if call[0][:3] != ("uci", "-q", "show")]
