from pathlib import Path

from open_omada_device_agent.config import PLACEHOLDER_MAC, default_device_mac, mac_is_random


def _sys(root: Path, **interfaces: str) -> Path:
    """Fake /sys/class/net: name -> "mac" or "mac,addr_assign_type"."""
    sys = root / "net"
    for name, value in interfaces.items():
        mac, _, kind = value.partition(",")
        (sys / name).mkdir(parents=True)
        (sys / name / "address").write_text(mac + "\n")
        if kind:
            (sys / name / "addr_assign_type").write_text(kind + "\n")
    return sys


def test_configured_mac_wins(tmp_path):
    sys = _sys(tmp_path, **{"br-lan": "02:aa:bb:00:00:01"})
    mac_file = tmp_path / "mac"
    assert default_device_mac({"OMADA_DEVICE_MAC": "02:11:22:33:44:55"}, sys, mac_file) == "02:11:22:33:44:55"
    assert not mac_file.exists()


def test_lan_bridge_mac_is_the_default_and_is_remembered(tmp_path):
    sys = _sys(tmp_path, **{"br-lan": "02:AA:BB:00:00:01", "br-guest": "02:aa:bb:00:00:02"})
    mac_file = tmp_path / "mac"

    assert default_device_mac({}, sys, mac_file) == "02:aa:bb:00:00:01"
    assert mac_file.read_text().strip() == "02:aa:bb:00:00:01"

    # After a reboot the bridge has a new random MAC; the saved one is kept.
    (sys / "br-lan" / "address").write_text("02:cc:dd:00:00:09\n")
    assert default_device_mac({"OMADA_DEVICE_MAC": " "}, sys, mac_file) == "02:aa:bb:00:00:01"


def test_other_bridge_and_bad_saved_value(tmp_path):
    sys = _sys(tmp_path, **{"br-lan": "02:aa:bb:00:00:01", "br-guest": "02:aa:bb:00:00:02"})
    mac_file = tmp_path / "mac"
    mac_file.write_text("not-a-mac\n")
    assert default_device_mac({"OMADA_OPENWRT_LAN_BRIDGE": "br-guest"}, sys, mac_file) == "02:aa:bb:00:00:02"


def test_missing_or_zero_bridge_falls_back_to_placeholder(tmp_path):
    mac_file = tmp_path / "mac"
    assert default_device_mac({}, tmp_path / "none", mac_file) == PLACEHOLDER_MAC
    sys = _sys(tmp_path, **{"br-lan": "00:00:00:00:00:00"})
    assert default_device_mac({}, sys, mac_file) == PLACEHOLDER_MAC
    assert not mac_file.exists()  # the placeholder is never remembered


def test_random_mac_detected_through_bridge_port(tmp_path):
    sys = _sys(tmp_path, **{"br-lan": "02:aa:bb:00:00:01,3", "lan1": "02:aa:bb:00:00:01,1"})
    (sys / "br-lan" / "brif").mkdir()
    (sys / "br-lan" / "brif" / "lan1").mkdir()
    assert mac_is_random("br-lan", sys)

    fixed = _sys(tmp_path / "fixed", **{"br-lan": "02:aa:bb:00:00:01,3", "lan1": "02:aa:bb:00:00:01,0"})
    (fixed / "br-lan" / "brif").mkdir()
    (fixed / "br-lan" / "brif" / "lan1").mkdir()
    assert not mac_is_random("br-lan", fixed)
