from pathlib import Path

import pytest

from open_omada_device_agent.adapters.inbound.ecsp.mappers.configuration import (
    parse_config_body,
)
from open_omada_device_agent.adapters.outbound.openwrt.capabilities import (
    detect_platform_capabilities,
)
from open_omada_device_agent.adapters.outbound.openwrt.ip_settings import (
    OpenWrtIpSettings,
    build_ip_batch,
    current_lan_setting,
    lan_setting_report,
    parse_interface_show,
    validate_lan_setting,
)
from open_omada_device_agent.adapters.outbound.openwrt.uci import CommandResult
from open_omada_device_agent.application.contracts import PlatformCapabilities
from open_omada_device_agent.contexts.networking.domain import LanIpSetting

STATIC_SHOW = """\
network.lan=interface
network.lan.device='br-lan'
network.lan.proto='static'
network.lan.ipaddr='192.0.2.2/24'
network.lan.gateway='192.0.2.1'
network.lan.dns='192.0.2.1'
"""

DHCP_SHOW = """\
network.lan=interface
network.lan.device='br-lan'
network.lan.proto='dhcp'
"""


def _static(**fields):
    body = {
        "connType": 0,
        "ipAddr": "192.0.2.20",
        "netMask": "255.255.255.0",
        "gateway": "192.0.2.1",
        "preferredDNS": "192.0.2.1",
        "alternateDNS": "",
    }
    body.update(fields)
    return parse_config_body({"lanSetting": body})


def _caps(**overrides):
    values = {"platform": "openwrt", "supports_ip_settings": True}
    values.update(overrides)
    return PlatformCapabilities(**values)


def test_mapper_reads_static_and_dhcp_modes():
    static = _static().lan_ip
    assert static == LanIpSetting(
        mode="static",
        ip_address="192.0.2.20",
        netmask="255.255.255.0",
        gateway="192.0.2.1",
        dns=("192.0.2.1",),
    )
    dhcp = parse_config_body(
        {"lanSetting": {"connType": 1, "useFallBack": True, "fallBackIp": "192.168.0.254"}}
    ).lan_ip
    assert dhcp.mode == "dhcp" and dhcp.fallback_enabled and dhcp.fallback_ip == "192.168.0.254"


def test_unknown_conn_type_leaves_the_address_alone():
    update = parse_config_body({"lanSetting": {"connType": 7}})
    assert update.lan_ip is None and update.unhandled_keys == ()


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"ipAddr": ""}, "need an IP address"),
        ({"netMask": "255.0.255.0"}, "invalid"),
        ({"ipAddr": "192.0.2.0"}, "network or broadcast"),
        ({"gateway": "198.51.100.1"}, "not another address"),
        ({"gateway": "192.0.2.20"}, "not another address"),
        ({"preferredDNS": "dns.example"}, "invalid DNS"),
    ],
)
def test_bad_static_settings_are_rejected(fields, message):
    assert message in validate_lan_setting(_static(**fields).lan_ip)


def test_static_batch_uses_cidr_and_replaces_dns():
    options = parse_interface_show(STATIC_SHOW, "lan")
    lines = build_ip_batch(_static(alternateDNS="192.0.2.53").lan_ip, options, "lan")

    assert lines == [
        "set network.lan.proto='static'",
        "set network.lan.ipaddr='192.0.2.20/24'",
        "set network.lan.gateway='192.0.2.1'",
        "delete network.lan.dns",
        "add_list network.lan.dns='192.0.2.1'",
        "add_list network.lan.dns='192.0.2.53'",
    ]


def test_matching_static_config_needs_no_change():
    options = parse_interface_show(STATIC_SHOW, "lan")
    assert build_ip_batch(_static(ipAddr="192.0.2.2").lan_ip, options, "lan") == []


def test_dhcp_batch_drops_static_options():
    options = parse_interface_show(STATIC_SHOW, "lan")
    lines = build_ip_batch(LanIpSetting(mode="dhcp"), options, "lan")

    assert lines == [
        "set network.lan.proto='dhcp'",
        "delete network.lan.ipaddr",
        "delete network.lan.gateway",
        "delete network.lan.dns",
    ]
    assert build_ip_batch(LanIpSetting(mode="dhcp"), parse_interface_show(DHCP_SHOW, "lan"), "lan") == []


def test_report_matches_the_running_address():
    static = current_lan_setting(parse_interface_show(STATIC_SHOW, "lan"))
    assert lan_setting_report(static) == {
        "connType": 0,
        "ipAddr": "192.0.2.2",
        "netMask": "255.255.255.0",
        "gateway": "192.0.2.1",
        "preferredDNS": "192.0.2.1",
    }
    dhcp = current_lan_setting(parse_interface_show(DHCP_SHOW, "lan"))
    assert lan_setting_report(dhcp) == {"connType": 1, "useFallBack": False}
    legacy = current_lan_setting({"proto": "static", "ipaddr": "192.0.2.2", "netmask": "255.255.0.0"})
    assert legacy.netmask == "255.255.0.0"


class _Runner:
    def __init__(self, show=STATIC_SHOW, batch_rc=0):
        self.show = show
        self.batch_rc = batch_rc
        self.calls = []
        self.batch = None

    def run(self, args, *, input_text=None):
        self.calls.append(list(args))
        if args[:3] == ["uci", "-q", "show"]:
            return CommandResult(0, self.show)
        if args[:3] == ["uci", "-q", "batch"]:
            self.batch = input_text
            return CommandResult(self.batch_rc)
        return CommandResult(0)


class _Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def _adapter(tmp_path: Path, runner, *, reachable, scheduled):
    network = tmp_path / "network"
    network.write_text("config interface 'lan'\n")
    clock = _Clock()
    spawned = []
    resets = []
    adapter = OpenWrtIpSettings(
        runner=runner,
        controller_endpoint=lambda: ("192.0.2.10", 29814),
        connect=lambda host, port: reachable,
        schedule=scheduled.append,
        spawn=spawned.append,
        sleep=clock.sleep,
        clock=clock,
        network_config=network,
        state_dir=tmp_path / "state",
        confirm_timeout=60,
        on_address_changed=lambda: resets.append(clock.now),
    )
    adapter.resets = resets
    return adapter, spawned


def test_change_is_answered_first_then_applied_and_confirmed(tmp_path):
    runner = _Runner()
    scheduled = []
    adapter, spawned = _adapter(tmp_path, runner, reachable=True, scheduled=scheduled)

    result = adapter.reconcile(_static(), _caps())

    assert result.applied and result.changed
    assert runner.batch is None  # nothing touched until after SET_RESPONSE
    (apply,) = scheduled
    apply()

    assert runner.batch.rstrip().endswith("commit network")
    assert ["/etc/init.d/network", "reload"] in runner.calls
    (script,) = spawned
    assert "sleep 60" in script and "/etc/init.d/network reload" in script
    confirmed = list((tmp_path / "state").glob("confirmed.*"))
    rollbacks = list((tmp_path / "state").glob("network.*"))
    assert len(confirmed) == 1 and len(rollbacks) == 1
    assert rollbacks[0].read_text() == "config interface 'lan'\n"
    assert confirmed[0].name.split(".")[1] in script
    assert adapter.resets == [0.0]  # session reconnects from the new address


def test_unreachable_controller_leaves_rollback_to_the_watchdog(tmp_path):
    scheduled = []
    adapter, spawned = _adapter(tmp_path, _Runner(), reachable=False, scheduled=scheduled)

    adapter.reconcile(_static(), _caps())
    scheduled[0]()

    assert spawned and not list((tmp_path / "state").glob("confirmed.*"))
    # Once right after the change, once after the watchdog put the old address back.
    assert len(adapter.resets) == 2 and adapter.resets[1] >= 60


def test_failed_batch_reverts_without_watchdog_or_reload(tmp_path):
    runner = _Runner(batch_rc=1)
    scheduled = []
    adapter, spawned = _adapter(tmp_path, runner, reachable=True, scheduled=scheduled)

    adapter.reconcile(_static(), _caps())
    scheduled[0]()

    assert ["uci", "revert", "network"] in runner.calls
    assert ["/etc/init.d/network", "reload"] not in runner.calls
    assert spawned == []


def test_opt_in_unchanged_and_invalid_cases(tmp_path):
    scheduled = []
    runner = _Runner()
    adapter, _ = _adapter(tmp_path, runner, reachable=True, scheduled=scheduled)

    off = adapter.reconcile(_static(), _caps(supports_ip_settings=False))
    same = adapter.reconcile(_static(ipAddr="192.0.2.2"), _caps())
    bad = adapter.reconcile(_static(gateway="198.51.100.1"), _caps())

    assert off.applied and not off.changed
    assert same.applied and not same.changed
    assert not bad.applied and "gateway" in bad.error
    assert scheduled == []


def test_capability_is_opt_in():
    which = lambda name: f"/usr/bin/{name}" if name in {"uci", "ubus"} else None  # noqa: E731
    assert not detect_platform_capabilities(env={}, command_exists=which).supports_ip_settings
    assert detect_platform_capabilities(
        env={"OMADA_CAP_IP_SETTINGS": "true"}, command_exists=which
    ).supports_ip_settings


def test_dns_only_change_keeps_the_session(tmp_path):
    scheduled = []
    adapter, _ = _adapter(tmp_path, _Runner(), reachable=True, scheduled=scheduled)

    adapter.reconcile(_static(ipAddr="192.0.2.2", preferredDNS="192.0.2.53"), _caps())
    scheduled[0]()

    assert adapter.resets == []


def test_rolled_back_change_is_not_retried(tmp_path):
    scheduled = []
    adapter, spawned = _adapter(tmp_path, _Runner(), reachable=False, scheduled=scheduled)

    adapter.reconcile(_static(), _caps())
    scheduled[0]()
    (script,) = spawned
    # Run the watchdog's rollback step: it records the change it reverted.
    fingerprint = script.split("echo ")[1].split(" ")[0]
    (tmp_path / "state" / "rolled-back").write_text(fingerprint + "\n")

    again = adapter.reconcile(_static(), _caps())
    other = adapter.reconcile(_static(ipAddr="192.0.2.30"), _caps())

    assert again.applied and not again.changed
    assert other.changed and len(scheduled) == 2
    assert "rolled-back" in script and "> '" in script


def test_second_change_waits_for_the_pending_one(tmp_path):
    scheduled = []
    adapter, spawned = _adapter(tmp_path, _Runner(), reachable=False, scheduled=scheduled)

    first = adapter.reconcile(_static(), _caps())
    # Same SET again (retransmit or resync) before the first one ran.
    second = adapter.reconcile(_static(), _caps())

    assert first.changed and not second.changed
    assert len(scheduled) == 1  # only one watchdog, so its rollback cannot be undone
    scheduled[0]()  # apply, controller unreachable, watchdog takes over
    assert len(spawned) == 1

    later = adapter.reconcile(_static(ipAddr="192.0.2.30"), _caps())
    assert later.changed and len(scheduled) == 2


def test_failed_schedule_does_not_block_later_changes(tmp_path):
    def broken(job):
        raise RuntimeError("no threads")

    runner = _Runner()
    network = tmp_path / "network"
    network.write_text("config interface 'lan'\n")
    adapter = OpenWrtIpSettings(
        runner=runner,
        schedule=broken,
        network_config=network,
        state_dir=tmp_path / "state",
    )
    with pytest.raises(RuntimeError):
        adapter.reconcile(_static(), _caps())
    scheduled = []
    adapter._schedule = scheduled.append
    assert adapter.reconcile(_static(), _caps()).changed and len(scheduled) == 1


def _run_watchdog(tmp_path, network_config):
    import subprocess

    from open_omada_device_agent.adapters.outbound.openwrt.ip_settings import _watchdog_script

    rollback = tmp_path / "network.backup"
    rollback.write_text("config interface 'lan'  # old\n")
    rejected = tmp_path / "rolled-back"
    script = _watchdog_script(
        rollback=rollback,
        confirm=tmp_path / "confirmed",
        network_config=network_config,
        timeout=0,
        rejected=rejected,
        fingerprint="abc123",
    )
    subprocess.run(["/bin/sh", "-c", script], check=False, capture_output=True)
    return rollback, rejected


def test_watchdog_records_a_rollback_that_worked(tmp_path):
    network = tmp_path / "network"
    network.write_text("config interface 'lan'  # new\n")

    rollback, rejected = _run_watchdog(tmp_path, network)

    assert network.read_text().endswith("# old\n")
    assert rejected.read_text().strip() == "abc123"
    assert not rollback.exists()


def test_watchdog_keeps_the_copy_when_the_restore_fails(tmp_path):
    rollback, rejected = _run_watchdog(tmp_path, tmp_path / "missing-dir" / "network")

    assert not rejected.exists()  # the agent may retry; nothing was rolled back
    assert rollback.exists()  # kept for a manual restore
