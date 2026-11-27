from dataclasses import dataclass, field

from open_omada_device_agent.ap_config import parse_config_body
from open_omada_device_agent.openwrt import CommandResult
from open_omada_device_agent.platform_capabilities import PlatformCapabilities
from open_omada_device_agent.portal_runtime import OpenWrtPortalRuntime


# A safe AP: openNDS on a guest bridge, uplink on another device.
SAFE_UPLINK_ANSWERS = {
    ("uci", "-q", "get", "opennds.@opennds[0].gatewayinterface"): "br-guest\n",
    ("ip", "-4", "route", "show", "default"): "default via 192.0.2.1 dev wan\n",
}


@dataclass
class RecordingRunner:
    calls: list[tuple[tuple[str, ...], str | None]] = field(default_factory=list)
    table_exists: bool = False

    def run(self, args, *, input_text=None):
        command = tuple(args)
        self.calls.append((command, input_text))
        if command == ("nft", "list", "table", "inet", "openomada_portal"):
            return CommandResult(returncode=0 if self.table_exists else 1)
        if command in SAFE_UPLINK_ANSWERS:
            return CommandResult(returncode=0, stdout=SAFE_UPLINK_ANSWERS[command])
        return CommandResult(returncode=0)


def _caps(**overrides):
    values = {
        "platform": "openwrt",
        "has_nft": True,
        "max_ssids": 4,
        "supports_portal": True,
    }
    values.update(overrides)
    return PlatformCapabilities(**values)


def test_portal_runtime_applies_nft_policy_for_portal_wlan_and_free_policy():
    update = parse_config_body(
        {
            "ssid_2G": {"ssid": [{"ssidName": "guest", "portal": True}]},
            "portalFreePolicyConfig": {
                "portalFreePolicy": [
                    {"value": "192.0.2.10"},
                    {"value": "not-an-ip"},
                ],
            },
        }
    )
    runner = RecordingRunner(table_exists=False)

    result = OpenWrtPortalRuntime(
        runner,
        interface="wlan0",
        redirect_port=8080,
    ).reconcile(update, _caps())

    assert result.applied is True
    assert result.changed is True
    assert runner.calls[0][0] == ("nft", "list", "table", "inet", "openomada_portal")
    assert runner.calls[1][0] == ("nft", "-f", "-")
    assert "iifname \"wlan0\" tcp dport 80 redirect to :8080" in (
        runner.calls[1][1] or ""
    )
    assert "192.0.2.10" in (runner.calls[1][1] or "")
    assert "not-an-ip" not in (runner.calls[1][1] or "")


def test_portal_runtime_rejects_missing_interface():
    update = parse_config_body(
        {"ssid_2G": {"ssid": [{"ssidName": "guest", "portal": True}]}}
    )

    result = OpenWrtPortalRuntime(RecordingRunner(), interface="").reconcile(
        update,
        _caps(),
    )

    assert result.applied is False
    assert "OMADA_PORTAL_INTERFACE" in result.error


def test_portal_runtime_noops_without_portal_config():
    update = parse_config_body({"ssid_2G": {"ssid": [{"ssidName": "corp"}]}})
    runner = RecordingRunner()

    result = OpenWrtPortalRuntime(runner, interface="wlan0").reconcile(update, _caps())

    assert result.applied is True
    assert result.changed is False
    assert runner.calls == []


def test_portal_runtime_leaves_enforcement_to_opennds_when_available():
    update = parse_config_body(
        {"ssid_2G": {"ssid": [{"ssidName": "guest", "portal": True}]}}
    )
    runner = RecordingRunner()

    result = OpenWrtPortalRuntime(runner, interface="wlan0").reconcile(
        update,
        _caps(has_opennds=True, has_nft=False),
    )

    assert result.applied is True
    assert result.changed is False
    assert [command for command, _ in runner.calls] == [("ndsctl", "status")]


def test_portal_runtime_starts_opennds_when_the_first_portal_ssid_arrives():
    class _StoppedOpenNds(RecordingRunner):
        def run(self, args, *, input_text=None):
            if tuple(args) == ("ndsctl", "status"):
                self.calls.append((tuple(args), input_text))
                return CommandResult(returncode=1)
            return super().run(args, input_text=input_text)

    update = parse_config_body(
        {"ssid_2G": {"ssid": [{"ssidName": "guest", "portal": True}]}}
    )
    runner = _StoppedOpenNds()

    result = OpenWrtPortalRuntime(runner, interface="").reconcile(
        update,
        _caps(has_opennds=True, has_nft=False),
    )

    assert result.applied is True
    assert result.changed is True
    assert (("/etc/init.d/opennds", "restart"), None) in runner.calls


def test_portal_runtime_applies_free_policy_to_opennds_without_portal_wlan():
    update = parse_config_body(
        {
            "portalFreePolicyConfig": {
                "portalFreePolicy": [{"dstIp": "8.8.8.8", "dstMask": 32}],
                "urlPortalFreePolicy": [{"url": "mediabeach.com.br/portal/c00e9a43"}],
            }
        }
    )
    runner = RecordingRunner()

    result = OpenWrtPortalRuntime(runner, interface="").reconcile(
        update,
        _caps(has_opennds=True, has_nft=False),
    )

    assert result.applied is True
    assert result.changed is True
    assert (
        (
            "uci",
            "add_list",
            "opennds.@opennds[0].walledgarden_fqdn_list=mediabeach.com.br",
        ),
        None,
    ) in runner.calls
    assert (
        (
            "uci",
            "add_list",
            "opennds.@opennds[0].preauthenticated_users=allow all to 8.8.8.8/32",
        ),
        None,
    ) in runner.calls
    assert (
        ("uci", "set", "opennds.@opennds[0].login_option_enabled=3"),
        None,
    ) in runner.calls


def test_portal_runtime_applies_portal_config_redirect_to_opennds():
    update = parse_config_body(
        {
            "portalConfigList": [
                {
                    "externalPortalServer": "https://portal.example.com/login",
                }
            ],
        }
    )
    runner = RecordingRunner()

    result = OpenWrtPortalRuntime(runner, interface="").reconcile(
        update,
        _caps(has_opennds=True, has_nft=False),
    )

    assert result.applied is True
    assert result.changed is True
    themespec = next(
        text
        for command, text in runner.calls
        if command == ("tee", "/usr/lib/opennds/theme_openomada_redirect.sh")
    )
    assert "https://portal.example.com/login" in (themespec or "")


def test_portal_runtime_ignores_portal_wlan_beyond_platform_limit():
    update = parse_config_body(
        {
            "ssid_2G": {
                "ssid": [
                    {"ssidName": "corp"},
                    {"ssidName": "guest", "portal": True},
                ]
            }
        }
    )
    runner = RecordingRunner()

    result = OpenWrtPortalRuntime(runner, interface="wlan0").reconcile(
        update,
        _caps(max_ssids=1),
    )

    assert result.applied is True
    assert result.changed is False
    assert runner.calls == []


def test_portal_runtime_noops_for_free_policy_without_portal_wlan():
    update = parse_config_body(
        {"portalFreePolicyConfig": {"portalFreePolicy": [{"value": "192.0.2.10"}]}}
    )
    runner = RecordingRunner()

    result = OpenWrtPortalRuntime(runner, interface="wlan0").reconcile(
        update,
        _caps(supports_portal=False, has_nft=False),
    )

    assert result.applied is True
    assert result.changed is False
    assert runner.calls == []


@dataclass
class UplinkGatewayRunner(RecordingRunner):
    """openNDS gateway is br-lan, which also carries the default route."""

    def run(self, args, *, input_text=None):
        command = tuple(args)
        self.calls.append((command, input_text))
        if command == ("uci", "-q", "get", "opennds.@opennds[0].gatewayinterface"):
            return CommandResult(returncode=0, stdout="br-lan\n")
        if command[:3] == ("ip", "-4", "route"):
            return CommandResult(returncode=0, stdout="default via 192.0.2.1 dev br-lan\n")
        return CommandResult(returncode=0)


def test_portal_runtime_skips_free_policy_when_opennds_would_capture_the_uplink():
    # A full sync to an AP without a portal SSID still carries the free policy.
    update = parse_config_body(
        {
            "portalFreePolicyConfig": {"urlPortalFreePolicy": [{"url": "portal.example.com"}]},
            "ssid_2G": {"ssid": [{"ssidName": "corp"}]},
        }
    )
    runner = UplinkGatewayRunner()

    result = OpenWrtPortalRuntime(runner, interface="").reconcile(
        update,
        _caps(has_opennds=True, has_nft=False),
    )

    assert result.applied is True
    assert result.changed is False
    assert not any(command[0] in ("tee", "/etc/init.d/opennds") for command, _ in runner.calls)
    assert not any(command[:2] == ("uci", "add_list") for command, _ in runner.calls)


def test_portal_runtime_still_refuses_a_portal_ssid_when_opennds_is_on_the_uplink():
    update = parse_config_body(
        {
            "portalFreePolicyConfig": {"urlPortalFreePolicy": [{"url": "portal.example.com"}]},
            "ssid_2G": {"ssid": [{"ssidName": "guest", "portal": True}]},
        }
    )

    result = OpenWrtPortalRuntime(UplinkGatewayRunner(), interface="").reconcile(
        update,
        _caps(has_opennds=True, has_nft=False),
    )

    assert result.applied is False
    assert "uplink" in result.error


def test_portal_runtime_preflight_refuses_a_portal_ssid_when_opennds_is_on_the_uplink():
    # Runs before the UCI adapter, so the portal SSID is never written.
    update = parse_config_body({"ssid_2G": {"ssid": [{"ssidName": "guest", "portal": True}]}})

    error = OpenWrtPortalRuntime(UplinkGatewayRunner(), interface="").preflight(
        update,
        _caps(has_opennds=True, has_nft=False),
    )

    assert error is not None and "uplink" in error


def test_portal_runtime_preflight_passes_without_a_portal_ssid():
    update = parse_config_body({"ssid_2G": {"ssid": [{"ssidName": "corp"}]}})
    runner = UplinkGatewayRunner()

    error = OpenWrtPortalRuntime(runner, interface="").preflight(
        update,
        _caps(has_opennds=True, has_nft=False),
    )

    assert error is None
    assert runner.calls == []


def test_portal_runtime_preflight_passes_with_opennds_on_a_guest_bridge():
    update = parse_config_body({"ssid_2G": {"ssid": [{"ssidName": "guest", "portal": True}]}})

    error = OpenWrtPortalRuntime(RecordingRunner(), interface="").preflight(
        update,
        _caps(has_opennds=True, has_nft=False),
    )

    assert error is None


@dataclass
class UnsetGatewayRunner(RecordingRunner):
    """No openNDS gatewayinterface option; the default route is on br-lan."""

    def run(self, args, *, input_text=None):
        command = tuple(args)
        self.calls.append((command, input_text))
        if command == ("uci", "-q", "get", "opennds.@opennds[0].gatewayinterface"):
            return CommandResult(returncode=1)
        if command == ("ip", "-4", "route", "show", "default"):
            return CommandResult(returncode=0, stdout="default via 192.0.2.1 dev br-lan\n")
        return CommandResult(returncode=0)


def test_portal_runtime_treats_an_unset_opennds_gateway_as_br_lan():
    # openNDS falls back to br-lan, which is the uplink here: skip, don't restart.
    update = parse_config_body(
        {"portalFreePolicyConfig": {"urlPortalFreePolicy": [{"url": "portal.example.com"}]}}
    )
    runner = UnsetGatewayRunner()

    result = OpenWrtPortalRuntime(runner, interface="").reconcile(
        update,
        _caps(has_opennds=True, has_nft=False),
    )

    assert result.applied is True
    assert result.changed is False
    assert not any(command[0] == "/etc/init.d/opennds" for command, _ in runner.calls)
