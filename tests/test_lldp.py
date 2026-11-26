import json
from types import SimpleNamespace

from open_omada_device_agent.adapters.inbound.ecsp.mappers.configuration import (
    parse_config_body,
)
from open_omada_device_agent.adapters.outbound.openwrt.capabilities import (
    detect_platform_capabilities,
)
from open_omada_device_agent.adapters.outbound.openwrt.device_profile import (
    GenericOpenWrtAccessPointProfile,
)
from open_omada_device_agent.adapters.outbound.openwrt.lldp import (
    OpenWrtLldpAdapter,
    build_lldp_batch,
    parse_lldpd_show,
    parse_neighbours,
    wired_bridge_ports,
)
from open_omada_device_agent.adapters.outbound.openwrt.uci import CommandResult
from open_omada_device_agent.application.contracts import PlatformCapabilities
from open_omada_device_agent.capabilities import ap_components_v2
from open_omada_device_agent.projections.inform import InformAssembler, LanObservation

# OpenWrt lldpd package defaults.
DEFAULT_SHOW = """\
lldpd.config=lldpd
lldpd.config.enable_cdp='1'
lldpd.config.enable_fdp='1'
lldpd.config.enable_sonmp='1'
lldpd.config.enable_edp='1'
lldpd.config.lldp_class='4'
lldpd.config.interface='loopback' 'lan'
"""

# Shape of ``lldpcli -f json0 show neighbors`` with one switch on lan1.
NEIGHBOURS = {
    "lldp": [
        {
            "interface": [
                {
                    "name": "lan1",
                    "via": "LLDP",
                    "chassis": [
                        {
                            "id": [{"type": "mac", "value": "02:aa:bb:cc:dd:10"}],
                            "name": [{"value": "lab-switch"}],
                            "descr": [{"value": "Example switch 1.0"}],
                            "capability": [
                                {"type": "Bridge", "enabled": True},
                                {"type": "Router", "enabled": False},
                            ],
                        }
                    ],
                    "port": [
                        {
                            "id": [{"type": "ifname", "value": "gi1/0/5"}],
                            "descr": [{"value": "Port 5"}],
                        }
                    ],
                }
            ]
        }
    ]
}


def _caps(**overrides):
    values = {"platform": "openwrt", "supports_lldp": True}
    values.update(overrides)
    return PlatformCapabilities(**values)


def _sys(tmp_path):
    sys_net = tmp_path / "net"
    for port in ("lan1", "lan2", "phy0-ap0"):
        (sys_net / "br-lan" / "brif" / port).mkdir(parents=True)
        (sys_net / port).mkdir(parents=True, exist_ok=True)
    (sys_net / "phy0-ap0" / "phy80211").mkdir()
    return sys_net


class _Runner:
    def __init__(self, show=DEFAULT_SHOW, enabled=True, batch_rc=0, neighbours="{}", lldpcli_rc=0):
        self.show = show
        self.enabled = enabled
        self.batch_rc = batch_rc
        self.neighbours = neighbours
        self.lldpcli_rc = lldpcli_rc
        self.calls = []
        self.batch = None

    def run(self, args, *, input_text=None):
        self.calls.append(list(args))
        if args[:3] == ["uci", "-q", "show"]:
            return CommandResult(0, self.show)
        if args[:3] == ["uci", "-q", "batch"]:
            self.batch = input_text
            return CommandResult(self.batch_rc)
        if args[0] == "lldpcli":
            return CommandResult(self.lldpcli_rc, self.neighbours)
        if args[-1] == "enabled":
            return CommandResult(0 if self.enabled else 1)
        return CommandResult(0)


def _adapter(tmp_path, runner, clock=lambda: 0.0):
    return OpenWrtLldpAdapter(runner=runner, sys_net=_sys(tmp_path), clock=clock)


def test_mapper_parses_lldp_enable():
    on = parse_config_body({"lldp": {"enable": 1}})
    off = parse_config_body({"lldp": {"enable": 0}})

    assert on.unhandled_keys == () and on.lldp.enabled
    assert "lldp" not in on.passive_keys
    assert not off.lldp.enabled
    assert parse_config_body({}).lldp is None


def test_wired_ports_skip_wifi(tmp_path):
    assert wired_bridge_ports("br-lan", _sys(tmp_path)) == ("lan1", "lan2")
    assert wired_bridge_ports("br-missing", _sys(tmp_path / "x")) == ()


def test_batch_runs_lldp_only_on_the_wired_ports():
    lines = build_lldp_batch(parse_lldpd_show(DEFAULT_SHOW), ("lan1", "lan2"))

    assert lines == [
        "set lldpd.config.enable_lldp='1'",
        "set lldpd.config.enable_cdp='0'",
        "set lldpd.config.enable_fdp='0'",
        "set lldpd.config.enable_sonmp='0'",
        "set lldpd.config.enable_edp='0'",
        "delete lldpd.config.interface",
        "add_list lldpd.config.interface='lan1'",
        "add_list lldpd.config.interface='lan2'",
    ]


def test_batch_is_empty_when_already_applied():
    show = (
        "lldpd.config=lldpd\n"
        "lldpd.config.enable_lldp='1'\n"
        "lldpd.config.enable_cdp='0'\n"
        "lldpd.config.enable_fdp='0'\n"
        "lldpd.config.enable_sonmp='0'\n"
        "lldpd.config.enable_edp='0'\n"
        "lldpd.config.interface='lan1'\n"
    )
    assert build_lldp_batch(parse_lldpd_show(show), ("lan1",)) == []


def test_batch_creates_a_missing_section():
    lines = build_lldp_batch(parse_lldpd_show(""), ("lan1",))
    assert lines[0] == "set lldpd.config=lldpd"


def test_enable_writes_uci_and_restarts(tmp_path):
    runner = _Runner(enabled=False)
    result = _adapter(tmp_path, runner).reconcile(parse_config_body({"lldp": {"enable": 1}}), _caps())

    assert result.applied and result.changed
    assert runner.batch.endswith("commit lldpd\n")
    assert ["/etc/init.d/lldpd", "enable"] in runner.calls
    assert ["/etc/init.d/lldpd", "restart"] in runner.calls


def test_failed_batch_is_reverted(tmp_path):
    runner = _Runner(batch_rc=1)
    result = _adapter(tmp_path, runner).reconcile(parse_config_body({"lldp": {"enable": 1}}), _caps())

    assert not result.applied and "lldpd" in result.error
    assert ["uci", "revert", "lldpd"] in runner.calls


def test_disable_stops_lldpd(tmp_path):
    runner = _Runner(enabled=True)
    result = _adapter(tmp_path, runner).reconcile(parse_config_body({"lldp": {"enable": 0}}), _caps())

    assert result.applied and result.changed
    assert ["/etc/init.d/lldpd", "stop"] in runner.calls
    assert ["/etc/init.d/lldpd", "disable"] in runner.calls
    assert runner.batch is None


def test_without_lldpd_the_setting_is_only_acknowledged(tmp_path):
    runner = _Runner()
    result = _adapter(tmp_path, runner).reconcile(
        parse_config_body({"lldp": {"enable": 1}}), _caps(supports_lldp=False)
    )

    assert result.applied and not result.changed
    assert runner.calls == []


def test_neighbours_use_omada_fields():
    assert parse_neighbours(json.dumps(NEIGHBOURS)) == [
        {
            "chassisId": "02-AA-BB-CC-DD-10",
            "portId": "gi1/0/5",
            "name": "lab-switch",
            "description": "Example switch 1.0",
            "capabilities": "00000100",  # bridge only
        }
    ]
    assert parse_neighbours('{"lldp": [{}]}') == []
    assert parse_neighbours("not json") == []


def test_inform_item_reports_neighbours_under_the_lan_port(tmp_path):
    now = [0.0]
    runner = _Runner(neighbours=json.dumps(NEIGHBOURS))
    adapter = _adapter(tmp_path, runner, clock=lambda: now[0])

    item = adapter.inform_item()
    assert item["lldps"][0]["port"] == "LAN"
    assert item["lldps"][0]["neighbors"][0]["portId"] == "gi1/0/5"

    adapter.inform_item()
    assert sum(call[0] == "lldpcli" for call in runner.calls) == 1  # cached
    now[0] = 60.0
    runner.lldpcli_rc = 1  # lldpd stopped
    assert adapter.inform_item() is None


def test_inform_carries_lldp_only_when_given():
    def assembler(lldp):
        return InformAssembler(
            device_info=lambda: {},
            lan=LanObservation(1000, 1, "LAN"),
            clients=lambda: (),
            client_projection=lambda clients: [],
            wireless_projection=lambda: {},
            lldp=lldp,
        )

    item = {"lldps": [{"port": "LAN", "neighbors": []}]}
    assert assembler(lambda: item).build(need_reply=False, uptime=1)["lldp"] == item
    assert "lldp" not in assembler(lambda: None).build(need_reply=False, uptime=1)
    assert "lldp" not in assembler(None).build(need_reply=False, uptime=1)


def test_capability_follows_lldpd_and_is_advertised():
    def which(found):
        return lambda name: f"/usr/bin/{name}" if name in found else None

    base = {"uci", "ubus"}
    caps = detect_platform_capabilities(env={}, command_exists=which(base | {"lldpd", "lldpcli"}))
    assert caps.supports_lldp
    components = ap_components_v2(caps)
    assert components["lldp"] == "1.0" and components["lldpInform"] == "1.0"

    profile = GenericOpenWrtAccessPointProfile.__new__(GenericOpenWrtAccessPointProfile)
    profile._capabilities = caps
    profile._settings = SimpleNamespace()
    assert profile.dev_cap()["supportLldp"] == 1

    without = detect_platform_capabilities(env={}, command_exists=which(base))
    assert not without.supports_lldp
    assert "lldp" not in ap_components_v2(without)
    profile._capabilities = without
    assert "supportLldp" not in profile.dev_cap()
    off = detect_platform_capabilities(
        env={"OMADA_CAP_LLDP": "false"}, command_exists=which(base | {"lldpd", "lldpcli"})
    )
    assert not off.supports_lldp
