from dataclasses import dataclass, field

from open_omada_device_agent.domain import PortalClientState
from open_omada_device_agent.openwrt import CommandResult
from open_omada_device_agent.portal import PortalSession
from open_omada_device_agent.portal_enforcement import (
    NftablesPortalAdapter,
    PortalPolicy,
    build_nftables_rules,
)


@dataclass
class RecordingRunner:
    calls: list[tuple[tuple[str, ...], str | None]] = field(default_factory=list)

    def run(self, args, *, input_text=None):
        command = tuple(args)
        self.calls.append((command, input_text))
        return CommandResult(returncode=0)


@dataclass
class FailingLoadRunner:
    calls: list[tuple[tuple[str, ...], str | None]] = field(default_factory=list)

    def run(self, args, *, input_text=None):
        self.calls.append((tuple(args), input_text))
        return CommandResult(returncode=1, stderr="syntax error\n")


def test_build_nftables_rules_for_authenticated_and_blocked_clients():
    rules = build_nftables_rules(
        PortalPolicy(
            interface="br-guest",
            redirect_port=8080,
            walled_garden_ipv4=("192.0.2.10",),
        ),
        (
            PortalSession(mac="aa:bb:cc:dd:ee:ff", state=PortalClientState.AUTHENTICATED),
            PortalSession(mac="02:00:00:00:00:02", state=PortalClientState.BLOCKED),
        ),
    )

    assert "table inet openomada_portal" in rules
    assert "aa:bb:cc:dd:ee:ff" in rules
    assert "02:00:00:00:00:02" in rules
    assert "udp dport { 53, 67, 68 } accept" in rules
    assert "ip daddr @walled_garden_v4 accept" in rules
    assert rules.count("ip daddr @walled_garden_v4 accept") == 2
    assert "tcp dport 80 redirect to :8080" in rules
    assert "tcp dport 443 drop" in rules


def test_nftables_adapter_replaces_existing_table_without_shell():
    runner = RecordingRunner()
    adapter = NftablesPortalAdapter(runner)

    result = adapter.apply(
        PortalPolicy(interface="br-guest", redirect_port=8080),
        (),
    )

    assert result.applied is True
    # One nft transaction: the old table stays until the new one loads.
    assert [call[0] for call in runner.calls] == [("nft", "-f", "-")]
    script = runner.calls[0][1] or ""
    assert script.startswith(
        "table inet openomada_portal {}\ndelete table inet openomada_portal\ntable inet openomada_portal {\n"
    )
    assert "elements = {  }" not in script


def test_nftables_adapter_reports_a_failed_load():
    runner = FailingLoadRunner()

    result = NftablesPortalAdapter(runner).apply(PortalPolicy(interface="br-guest", redirect_port=8080), ())

    assert result.applied is False
    assert result.error == "syntax error"
    assert len(runner.calls) == 1


def test_nftables_adapter_rejects_invalid_interface():
    runner = RecordingRunner()
    adapter = NftablesPortalAdapter(runner)

    result = adapter.apply(
        PortalPolicy(interface="guest; rm -rf /", redirect_port=8080),
        (),
    )

    assert result.applied is False
    assert "invalid portal interface" in result.error
    assert runner.calls == []
