import base64
import io

import pytest

from open_omada_device_agent.adapters.inbound.ecsp.mappers.configuration import (
    parse_config_body,
)
from open_omada_device_agent.adapters.outbound.openwrt.network_check import (
    OpenWrtNetworkCheck,
    network_check_argv,
)
from open_omada_device_agent.application.contracts import PlatformCapabilities
from open_omada_device_agent.application.notifications import (
    NOTIFY_SUBJECT_TROUBLESHOOTING,
    OutboundNotifications,
)
from open_omada_device_agent.capabilities import ap_components_v2
from open_omada_device_agent.contexts.device.domain import NetworkCheckRequest


def _text(msg):
    """The controller UI decodes msg with atob, so it must be base64."""
    return base64.b64decode(msg, validate=True).decode("utf-8")


def _caps(**overrides):
    values = {"platform": "openwrt", "supports_network_check": True}
    values.update(overrides)
    return PlatformCapabilities(**values)


def test_mapper_reads_each_tool_key():
    update = parse_config_body(
        {
            "ping": {"cmdId": "c1", "dest": "192.0.2.1", "additional": {"cnt": 3, "pktSize": 100}},
            "arptable": {"cmdId": "c2"},
        }
    )

    assert update.unhandled_keys == ()
    assert update.network_checks == (
        NetworkCheckRequest(kind="ping", cmd_id="c1", dest="192.0.2.1", count=3, packet_size=100),
        NetworkCheckRequest(kind="arptable", cmd_id="c2"),
    )


@pytest.mark.parametrize(
    ("request_", "argv"),
    [
        (NetworkCheckRequest("ping", "c", "example.com", 3, 100),
         ["ping", "-c", "3", "-s", "100", "-W", "2", "example.com"]),
        (NetworkCheckRequest("ping", "c", "192.0.2.1", 0, 999999),
         ["ping", "-c", "4", "-s", "65500", "-W", "2", "192.0.2.1"]),
        (NetworkCheckRequest("traceroute", "c", "2001:db8::1"),
         ["traceroute", "-w", "2", "-m", "30", "2001:db8::1"]),
        (NetworkCheckRequest("dnslookup", "c", "example.com"), ["nslookup", "example.com"]),
        (NetworkCheckRequest("arptable", "c"), None),
    ],
)
def test_argv_per_tool(request_, argv):
    assert network_check_argv(request_) == argv


@pytest.mark.parametrize("dest", [None, "", "-f", "a b", "x;reboot", "$(id)"])
def test_unsafe_or_missing_destinations_are_rejected(dest):
    with pytest.raises(ValueError):
        network_check_argv(NetworkCheckRequest("ping", "c", dest))


class _Process:
    def __init__(self, output):
        self.stdout = io.StringIO(output)
        self.killed = False

    def kill(self):
        self.killed = True

    def wait(self):
        return 0


def _run_now(target):
    target()


def _checker(notifications, output="", **kwargs):
    calls = []

    def popen(argv, **_):
        calls.append(argv)
        return _Process(output)

    checker = OpenWrtNetworkCheck(notifications.put, popen=popen, start_thread=_run_now, **kwargs)
    return checker, calls


def test_ping_output_streams_as_numbered_chunks():
    notifications = OutboundNotifications()
    checker, calls = _checker(notifications, "PING x\n64 bytes\n--- stats ---\n")
    update = parse_config_body({"ping": {"cmdId": "c9", "dest": "192.0.2.1"}})

    result = checker.reconcile(update, _caps())

    assert result.applied
    assert calls == [["ping", "-c", "4", "-s", "56", "-W", "2", "192.0.2.1"]]
    sent = notifications.drain()
    assert {item.subject for item in sent} == {NOTIFY_SUBJECT_TROUBLESHOOTING}
    chunks = [item.content["troubleshooting"] for item in sent]
    assert [item.content["cmdId"] for item in sent] == ["c9"] * 3
    assert [_text(c["msg"]) for c in chunks] == ["PING x\n", "64 bytes\n", "--- stats ---\n"]
    assert [(c["seq"], c["bof"], c["finish"]) for c in chunks] == [(0, 1, 0), (1, 0, 0), (2, 0, 1)]


def test_arp_table_reads_proc_without_a_command(tmp_path):
    arp = tmp_path / "arp"
    arp.write_text("IP address HW type\n192.0.2.9 0x1\n")
    notifications = OutboundNotifications()
    checker, calls = _checker(notifications, arp_path=arp)

    checker.reconcile(parse_config_body({"arptable": {"cmdId": "a"}}), _caps())

    assert calls == []
    assert [_text(n.content["troubleshooting"]["msg"]) for n in notifications.drain()] == [
        "IP address HW type\n",
        "192.0.2.9 0x1\n",
    ]


def test_a_failing_tool_still_finishes_the_stream():
    notifications = OutboundNotifications()

    def popen(argv, **_):
        raise FileNotFoundError("traceroute")

    checker = OpenWrtNetworkCheck(notifications.put, popen=popen, start_thread=_run_now)
    checker.reconcile(parse_config_body({"traceroute": {"cmdId": "t", "dest": "example.com"}}), _caps())

    (only,) = notifications.drain()
    assert only.content["troubleshooting"]["finish"] == 1
    assert "failed" in _text(only.content["troubleshooting"]["msg"])


def test_bad_destination_fails_the_set_and_runs_nothing():
    notifications = OutboundNotifications()
    checker, calls = _checker(notifications)

    result = checker.reconcile(parse_config_body({"ping": {"cmdId": "c", "dest": "-x"}}), _caps())

    assert not result.applied
    assert calls == [] and notifications.drain() == []


def test_unsupported_platform_rejects_and_other_sets_are_ignored():
    notifications = OutboundNotifications()
    checker, _ = _checker(notifications)
    update = parse_config_body({"ping": {"cmdId": "c", "dest": "192.0.2.1"}})

    assert not checker.reconcile(update, _caps(supports_network_check=False)).applied
    assert checker.reconcile(parse_config_body({"led": {"enable": True}}), _caps()).changed is False


def test_components_advertise_network_check_tools():
    components = ap_components_v2(_caps())
    assert {key: components[key] for key in ("ping", "traceroute", "dnslookup", "arptable")} == {
        "ping": "1.0",
        "traceroute": "1.0",
        "dnslookup": "1.0",
        "arptable": "1.0",
    }
    assert "dnslookup" not in ap_components_v2(_caps(supports_network_check=False))


def test_notification_queue_is_bounded():
    notifications = OutboundNotifications(max_pending=2)
    for index in range(3):
        notifications.put(5, {"i": index})

    assert [item.content["i"] for item in notifications.drain()] == [0, 1]
