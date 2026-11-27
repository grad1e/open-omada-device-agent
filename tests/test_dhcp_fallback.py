import json

from open_omada_device_agent.adapters.outbound.openwrt.dhcp_fallback import DhcpFallback
from open_omada_device_agent.adapters.outbound.openwrt.uci import CommandResult

FALLBACK_SHOW = """\
network.lan=interface
network.lan.device='br-lan'
network.lan.proto='dhcp'
network.lan.openomada_fallback='192.0.2.254/24'
network.lan.openomada_fallback_gateway='192.0.2.1'
"""


class _Ap:
    """Fake uci/ubus/ip for one interface."""

    def __init__(self, show=FALLBACK_SHOW):
        self.show = show
        self.lease = None
        self.default_route = ""
        self.ip = []

    def run(self, args, *, input_text=None):
        if args[:3] == ["uci", "-q", "show"]:
            return CommandResult(0, self.show)
        if args[0] == "ubus":
            status = {"device": "br-lan", "ipv4-address": []}
            if self.lease:
                status["l3_device"] = "br-lan"
                status["ipv4-address"] = [{"address": self.lease, "mask": 24}]
            return CommandResult(0, json.dumps(status))
        if args[:4] == ["ip", "-4", "route", "show"]:
            return CommandResult(0, self.default_route)
        self.ip.append(" ".join(args[2:]))
        return CommandResult(0)


def _fallback(tmp_path, ap):
    clock = {"now": 0.0}
    changes = []
    fallback = DhcpFallback(
        runner=ap,
        clock=lambda: clock["now"],
        state_file=tmp_path / "fallback",
        on_change=lambda: changes.append(clock["now"]),
        delay=30,
        ipv4_conf=tmp_path / "conf",
    )
    (tmp_path / "conf" / "br-lan").mkdir(parents=True, exist_ok=True)
    return fallback, clock, changes


def test_fallback_after_30s_without_lease_and_removed_on_lease(tmp_path):
    ap = _Ap()
    fallback, clock, changes = _fallback(tmp_path, ap)

    for now in (0, 10, 25):
        clock["now"] = now
        fallback.check()
    assert ap.ip == []  # still waiting for DHCP

    clock["now"] = 30
    fallback.check()
    assert ap.ip == [
        "addr add 192.0.2.254/24 dev br-lan",
        "route add default via 192.0.2.1 dev br-lan metric 1000",
    ]
    clock["now"] = 35
    fallback.check()
    assert len(ap.ip) == 2  # added once

    ap.lease = "192.0.2.130"
    fallback.check()
    assert ap.ip[2:] == [
        "route del default via 192.0.2.1 dev br-lan metric 1000",
        "addr del 192.0.2.254/24 dev br-lan",
    ]
    assert changes == [30, 35]
    # A lease in the same subnet is a secondary address; keep it when the
    # fallback (primary) goes.
    assert (tmp_path / "conf" / "br-lan" / "promote_secondaries").read_text() == "1\n"
    assert not (tmp_path / "fallback").exists()


def test_existing_default_route_is_left_alone(tmp_path):
    ap = _Ap()
    ap.default_route = "default via 198.51.100.1 dev eth1"
    fallback, clock, _ = _fallback(tmp_path, ap)
    fallback.check()
    clock["now"] = 30
    fallback.check()
    assert ap.ip == ["addr add 192.0.2.254/24 dev br-lan"]

    ap.show = FALLBACK_SHOW.replace("'dhcp'", "'static'")  # switched away from DHCP
    fallback.check()
    assert ap.ip[1:] == ["addr del 192.0.2.254/24 dev br-lan"]


def test_nothing_happens_without_a_configured_fallback(tmp_path):
    ap = _Ap(show="network.lan=interface\nnetwork.lan.proto='dhcp'\n")
    fallback, clock, changes = _fallback(tmp_path, ap)
    for now in (0, 60, 120):
        clock["now"] = now
        fallback.check()
    assert ap.ip == [] and changes == []


def test_fallback_left_by_a_previous_run_is_cleaned_up(tmp_path):
    (tmp_path / "fallback").write_text("192.0.2.254/24 br-lan -\n")
    ap = _Ap()
    ap.lease = "192.0.2.130"
    fallback, _, _ = _fallback(tmp_path, ap)
    fallback.check()
    assert ap.ip == ["addr del 192.0.2.254/24 dev br-lan"]


def test_failed_address_add_is_not_recorded_and_retried_later(tmp_path):
    ap = _Ap()
    real_run = ap.run
    failures = [1]

    def run(args, *, input_text=None):
        if args[:4] == ["ip", "-4", "addr", "add"] and failures:
            failures.pop()
            ap.ip.append("failed add")
            return CommandResult(2, "", "RTNETLINK answers: Invalid argument\n")
        return real_run(args, input_text=input_text)

    ap.run = run
    fallback, clock, changes = _fallback(tmp_path, ap)
    fallback.check()
    clock["now"] = 30
    fallback.check()
    assert ap.ip == ["failed add"]  # no route, no state, no change event
    assert not (tmp_path / "fallback").exists() and changes == []

    clock["now"] = 35
    fallback.check()
    assert ap.ip == ["failed add"]  # waits another delay instead of retrying every check
    clock["now"] = 60
    fallback.check()
    assert ap.ip[1:] == [
        "addr add 192.0.2.254/24 dev br-lan",
        "route add default via 192.0.2.1 dev br-lan metric 1000",
    ]
    assert changes == [60]


def test_fallback_only_edit_swaps_the_active_address(tmp_path):
    # DHCP -> DHCP with a new fallback address while the fallback is in use.
    ap = _Ap()
    fallback, clock, changes = _fallback(tmp_path, ap)
    fallback.check()
    clock["now"] = 30
    fallback.check()

    ap.show = FALLBACK_SHOW.replace("192.0.2.254/24", "192.0.2.253/24")
    clock["now"] = 35
    fallback.check()
    assert ap.ip[2:] == [
        "route del default via 192.0.2.1 dev br-lan metric 1000",
        "addr del 192.0.2.254/24 dev br-lan",
        "addr add 192.0.2.253/24 dev br-lan",
        "route add default via 192.0.2.1 dev br-lan metric 1000",
    ]
    assert (tmp_path / "fallback").read_text().startswith("192.0.2.253/24 br-lan")
    assert changes == [30, 35]
