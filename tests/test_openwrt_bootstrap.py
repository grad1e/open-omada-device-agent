from dataclasses import dataclass, field

from open_omada_device_agent.adapters.outbound.openwrt.bootstrap import (
    OpenWrtBootstrapConfig,
    OpenWrtStartupBootstrap,
)
from open_omada_device_agent.openwrt import CommandResult
from open_omada_device_agent.platform_capabilities import PlatformCapabilities


@dataclass
class BootstrapRunner:
    responses: dict[tuple[str, ...], CommandResult] = field(default_factory=dict)
    calls: list[tuple[tuple[str, ...], str | None]] = field(default_factory=list)

    def run(self, args, *, input_text=None):
        command = tuple(args)
        self.calls.append((command, input_text))
        if command in self.responses:
            return self.responses[command]
        if command[:3] == ("uci", "-q", "get"):
            return CommandResult(returncode=1)
        if command[:3] == ("uci", "-q", "show"):
            return CommandResult(returncode=1)
        return CommandResult(returncode=0)


def _caps(**overrides):
    values = {
        "platform": "openwrt",
        "has_uci": True,
        "has_opennds": False,
    }
    values.update(overrides)
    return PlatformCapabilities(**values)


def _batch_payloads(runner: BootstrapRunner) -> list[str]:
    return [
        input_text or ""
        for command, input_text in runner.calls
        if command == ("uci", "-q", "batch")
    ]


def test_bootstrap_enables_empty_br_lan_and_reloads_network():
    runner = BootstrapRunner(
        responses={
            ("uci", "-q", "show", "network"): CommandResult(
                returncode=0,
                stdout="network.@device[0]=device\nnetwork.@device[0].name='br-lan'\n",
            ),
            ("uci", "-q", "get", "network.@device[0].type"): CommandResult(
                returncode=0,
                stdout="bridge\n",
            ),
            ("uci", "-q", "get", "network.lan"): CommandResult(
                returncode=0,
                stdout="interface\n",
            ),
            ("uci", "-q", "get", "network.lan.device"): CommandResult(
                returncode=0,
                stdout="br-lan\n",
            ),
            ("uci", "-q", "get", "network.lan.proto"): CommandResult(
                returncode=0,
                stdout="static\n",
            ),
            ("uci", "-q", "get", "network.lan.ipaddr"): CommandResult(
                returncode=0,
                stdout="192.168.1.1/24\n",
            ),
        }
    )

    result = OpenWrtStartupBootstrap(runner).apply(_caps())

    assert result.changed is True
    batch = _batch_payloads(runner)[0]
    assert "set network.@device[0].bridge_empty='1'" in batch
    assert "commit network" in batch
    assert (("/etc/init.d/network", "reload"), None) in runner.calls
    assert (("wifi", "reload"), None) in runner.calls


def test_bootstrap_creates_minimal_lan_bridge_when_missing():
    runner = BootstrapRunner(
        responses={
            ("uci", "-q", "show", "network"): CommandResult(returncode=0, stdout=""),
        }
    )

    OpenWrtStartupBootstrap(runner).apply(_caps())

    batch = _batch_payloads(runner)[0]
    assert "set network.openomada_br_lan=device" in batch
    assert "set network.openomada_br_lan.name='br-lan'" in batch
    assert "set network.openomada_br_lan.type='bridge'" in batch
    assert "set network.openomada_br_lan.bridge_empty='1'" in batch
    assert "set network.lan=interface" in batch
    assert "set network.lan.device='br-lan'" in batch
    assert "set network.lan.proto='static'" in batch
    assert "set network.lan.ipaddr='192.168.1.1/24'" in batch


def test_bootstrap_configures_opennds_base_and_disables_preemptive_auth():
    runner = BootstrapRunner(
        responses={
            # A router: the uplink is wan, so openNDS may run on br-lan.
            ("ip", "-4", "route", "show", "default"): CommandResult(
                returncode=0, stdout="default via 198.51.100.1 dev wan\n"
            ),
            ("uci", "-q", "get", "opennds.@opennds[0]"): CommandResult(
                returncode=0,
                stdout="opennds\n",
            ),
            ("uci", "-q", "get", "opennds.@opennds[0].gatewayport"): CommandResult(
                returncode=0,
                stdout="2050\n",
            ),
            ("uci", "-q", "get", "opennds.@opennds[0].gatewayfqdn"): CommandResult(
                returncode=0,
                stdout="status.client\n",
            ),
        }
    )
    bootstrap = OpenWrtStartupBootstrap(
        runner,
        config=OpenWrtBootstrapConfig(
            ensure_lan=False,
            opennds_gateway_name="OpenOmada Ubatuba",
        ),
    )

    result = bootstrap.apply(_caps(has_opennds=True))

    assert result.changed is True
    batch = _batch_payloads(runner)[0]
    assert "set opennds.@opennds[0].enabled='1'" in batch
    assert "set opennds.@opennds[0].gatewayinterface='br-lan'" in batch
    assert "set opennds.@opennds[0].gatewayname='OpenOmada Ubatuba'" in batch
    assert "set opennds.@opennds[0].gatewayfqdn='disable'" in batch
    assert "set opennds.@opennds[0].allow_preemptive_authentication='0'" in batch
    assert (("/etc/init.d/opennds", "restart"), None) in runner.calls


def test_bootstrap_starts_opennds_when_base_config_is_already_current():
    runner = BootstrapRunner(
        responses={
            # A router: the uplink is wan, so openNDS may run on br-lan.
            ("ip", "-4", "route", "show", "default"): CommandResult(
                returncode=0, stdout="default via 198.51.100.1 dev wan\n"
            ),
            ("uci", "-q", "get", "opennds.@opennds[0]"): CommandResult(
                returncode=0,
                stdout="opennds\n",
            ),
            ("uci", "-q", "get", "opennds.@opennds[0].enabled"): CommandResult(
                returncode=0,
                stdout="1\n",
            ),
            ("uci", "-q", "get", "opennds.@opennds[0].gatewayinterface"): CommandResult(
                returncode=0,
                stdout="br-lan\n",
            ),
            ("uci", "-q", "get", "opennds.@opennds[0].gatewayport"): CommandResult(
                returncode=0,
                stdout="2050\n",
            ),
            ("uci", "-q", "get", "opennds.@opennds[0].gatewayname"): CommandResult(
                returncode=0,
                stdout="OpenOmada-AP\n",
            ),
            ("uci", "-q", "get", "opennds.@opennds[0].gatewayfqdn"): CommandResult(
                returncode=0,
                stdout="disable\n",
            ),
            (
                "uci",
                "-q",
                "get",
                "opennds.@opennds[0].allow_preemptive_authentication",
            ): CommandResult(returncode=0, stdout="0\n"),
        }
    )
    bootstrap = OpenWrtStartupBootstrap(
        runner,
        config=OpenWrtBootstrapConfig(ensure_lan=False),
    )

    result = bootstrap.apply(_caps(has_opennds=True))

    assert result.changed is False
    assert _batch_payloads(runner) == []
    assert (("/etc/init.d/opennds", "start"), None) in runner.calls


def test_bootstrap_adds_wan_management_rules_only_when_enabled():
    runner = BootstrapRunner()
    bootstrap = OpenWrtStartupBootstrap(
        runner,
        config=OpenWrtBootstrapConfig(
            ensure_lan=False,
            ensure_opennds=False,
            enable_wan_management=True,
        ),
    )

    result = bootstrap.apply(_caps())

    assert result.changed is True
    batch = _batch_payloads(runner)[0]
    assert "set firewall.openomada_allow_ssh_wan=rule" in batch
    assert "set firewall.openomada_allow_ssh_wan.dest_port='22'" in batch
    assert "set firewall.openomada_allow_luci_http_wan.dest_port='80'" in batch
    assert "set firewall.openomada_allow_luci_https_wan.dest_port='443'" in batch
    assert (("/etc/init.d/firewall", "reload"), None) in runner.calls


def test_bootstrap_noops_outside_openwrt():
    runner = BootstrapRunner()

    result = OpenWrtStartupBootstrap(runner).apply(PlatformCapabilities(platform="generic"))

    assert result.changed is False
    assert runner.calls == []


WIRELESS_SHOW = CommandResult(
    returncode=0,
    stdout=(
        "wireless.radio0=wifi-device\n"
        "wireless.radio0.band='2g'\n"
        "wireless.radio1=wifi-device\n"
        "wireless.radio1.band='5g'\n"
        "wireless.default_radio0=wifi-iface\n"
    ),
)


def _wifi_only(**overrides):
    return OpenWrtBootstrapConfig(ensure_lan=False, ensure_opennds=False, **overrides)


def test_bootstrap_sets_wifi_country_on_every_radio_and_reloads_wifi(caplog):
    caplog.set_level("INFO")
    runner = BootstrapRunner(
        responses={
            ("uci", "-q", "show", "wireless"): WIRELESS_SHOW,
            ("uci", "-q", "get", "wireless.radio1.country"): CommandResult(
                returncode=0, stdout="IN\n"
            ),
        }
    )

    result = OpenWrtStartupBootstrap(runner, config=_wifi_only(wifi_country="IN")).apply(
        _caps()
    )

    assert result.changed is True
    assert result.wireless_changed is True
    batch = _batch_payloads(runner)[0]
    assert "set wireless.radio0.country='IN'" in batch
    assert "radio1" not in batch  # already correct
    assert "Set Wi-Fi country IN on radio0" in caplog.text
    assert "radio0, radio1" not in caplog.text
    assert "commit wireless" in batch
    assert (("wifi", "reload"), None) in runner.calls


def test_bootstrap_leaves_matching_wifi_country_alone():
    current = CommandResult(returncode=0, stdout="IN\n")
    runner = BootstrapRunner(
        responses={
            ("uci", "-q", "show", "wireless"): WIRELESS_SHOW,
            ("uci", "-q", "get", "wireless.radio0.country"): current,
            ("uci", "-q", "get", "wireless.radio1.country"): current,
        }
    )

    result = OpenWrtStartupBootstrap(runner, config=_wifi_only(wifi_country="IN")).apply(
        _caps()
    )

    assert result.changed is False
    assert result.wireless_changed is False
    assert _batch_payloads(runner) == []
    assert (("wifi", "reload"), None) not in runner.calls


def test_bootstrap_warns_when_radios_have_no_country_and_none_is_configured():
    runner = BootstrapRunner(responses={("uci", "-q", "show", "wireless"): WIRELESS_SHOW})

    result = OpenWrtStartupBootstrap(runner, config=_wifi_only()).apply(_caps())

    assert result.changed is False
    assert _batch_payloads(runner) == []
    assert any(
        "radio0, radio1 have no country code" in warning and "OMADA_WIFI_COUNTRY" in warning
        for warning in result.warnings
    )


@dataclass
class _RegdomainRunner(BootstrapRunner):
    reg_outputs: list[str] = field(default_factory=list)

    def run(self, args, *, input_text=None):
        if tuple(args) == ("iw", "reg", "get"):
            self.calls.append((tuple(args), input_text))
            stdout = self.reg_outputs.pop(0) if len(self.reg_outputs) > 1 else self.reg_outputs[0]
            return CommandResult(returncode=0, stdout=stdout)
        return super().run(args, input_text=input_text)


def test_bootstrap_waits_for_the_kernel_to_apply_the_new_country():
    runner = _RegdomainRunner(
        responses={("uci", "-q", "show", "wireless"): WIRELESS_SHOW},
        reg_outputs=["global\ncountry IN: DFS-UNSET\n"] * 3 + ["global\ncountry PA: DFS-FCC\n"],
    )
    sleeps = []

    result = OpenWrtStartupBootstrap(
        runner, config=_wifi_only(wifi_country="PA"), sleep=sleeps.append
    ).apply(_caps())

    assert result.wireless_changed is True
    assert len(sleeps) == 3
    assert result.warnings == ()


def test_bootstrap_warns_when_the_country_never_applies():
    runner = _RegdomainRunner(
        responses={("uci", "-q", "show", "wireless"): WIRELESS_SHOW},
        reg_outputs=["global\ncountry IN: DFS-UNSET\n"],
    )

    result = OpenWrtStartupBootstrap(
        runner, config=_wifi_only(wifi_country="PA"), sleep=lambda _s: None
    ).apply(_caps())

    assert any("did not switch to PA" in w for w in result.warnings)


def _prune_caps():
    from open_omada_device_agent.contexts.wireless.domain import (
        RadioBand,
        RadioChannel,
        RadioHardware,
    )

    hardware = RadioHardware(
        band=RadioBand.FIVE_G,
        channels=(
            RadioChannel(36, 5180),
            RadioChannel(140, 5700, radar=True),
            RadioChannel(149, 5745),
        ),
    )
    return _caps(radio_hardware=(hardware,))


def _prune_runner(channels):
    return BootstrapRunner(
        responses={
            ("uci", "-q", "show", "wireless"): WIRELESS_SHOW,
            ("uci", "-q", "get", "wireless.radio1.band"): CommandResult(returncode=0, stdout="5g\n"),
            ("uci", "-q", "get", "wireless.radio1.channels"): CommandResult(
                returncode=0, stdout=f"{channels}\n"
            ),
        }
    )


def test_startup_prunes_radar_channels_from_a_stale_auto_list(caplog):
    caplog.set_level("INFO")
    runner = _prune_runner("36 140 149")

    result = OpenWrtStartupBootstrap(runner, config=_wifi_only()).prune_auto_channels(_prune_caps())

    assert result.changed is True
    assert "set wireless.radio1.channels='36 149'" in _batch_payloads(runner)[0]
    assert (("wifi", "reload"), None) in runner.calls
    assert "radio1 -> 36 149" in caplog.text


def test_startup_leaves_a_usable_auto_list_alone():
    runner = _prune_runner("36 149")

    result = OpenWrtStartupBootstrap(runner, config=_wifi_only()).prune_auto_channels(_prune_caps())

    assert result.changed is False
    assert (("wifi", "reload"), None) not in runner.calls


def test_startup_falls_back_to_usable_channels_when_none_survive():
    runner = _prune_runner("140")

    OpenWrtStartupBootstrap(runner, config=_wifi_only()).prune_auto_channels(_prune_caps())

    assert "set wireless.radio1.channels='36 149'" in _batch_payloads(runner)[0]


TRI_BAND_SHOW = CommandResult(
    returncode=0,
    stdout=(
        "wireless.radio0=wifi-device\n"
        "wireless.radio0.band='2g'\n"
        "wireless.radio1=wifi-device\n"
        "wireless.radio1.band='5g'\n"
        "wireless.radio2=wifi-device\n"
        "wireless.radio2.band='5g'\n"
    ),
)


def _tri_band_caps():
    from open_omada_device_agent.contexts.wireless.domain import (
        RadioBand,
        RadioChannel,
        RadioHardware,
    )

    low = RadioHardware(
        band=RadioBand.FIVE_G,
        channels=(RadioChannel(36, 5180), RadioChannel(40, 5200), RadioChannel(48, 5240)),
    )
    high = RadioHardware(
        band=RadioBand.FIVE_G2,
        channels=(RadioChannel(149, 5745), RadioChannel(157, 5785), RadioChannel(165, 5825)),
    )
    return _caps(radio_hardware=(low, high))


def _tri_band_runner(radio1, radio2):
    responses = {("uci", "-q", "show", "wireless"): TRI_BAND_SHOW}
    for radio, band, channels in (("radio0", "2g", ""), ("radio1", "5g", radio1), ("radio2", "5g", radio2)):
        responses[("uci", "-q", "get", f"wireless.{radio}.band")] = CommandResult(returncode=0, stdout=f"{band}\n")
        if channels:
            responses[("uci", "-q", "get", f"wireless.{radio}.channels")] = CommandResult(
                returncode=0, stdout=f"{channels}\n"
            )
    return BootstrapRunner(responses=responses)


def test_tri_band_second_5ghz_radio_keeps_its_own_channels():
    # radio2 is the high-band radio: before, it was checked against the
    # low-band radio's channels and its list was replaced with 36-48.
    runner = _tri_band_runner("36 40 48", "149 157 165")

    result = OpenWrtStartupBootstrap(runner, config=_wifi_only()).prune_auto_channels(_tri_band_caps())

    assert result.changed is False
    assert _batch_payloads(runner) == []


def test_tri_band_prunes_each_5ghz_radio_against_its_own_phy():
    runner = _tri_band_runner("36 40 149", "48 157 165")

    OpenWrtStartupBootstrap(runner, config=_wifi_only()).prune_auto_channels(_tri_band_caps())

    batch = _batch_payloads(runner)[0]
    assert "set wireless.radio1.channels='36 40'" in batch
    assert "set wireless.radio2.channels='157 165'" in batch


def test_tri_band_falls_back_to_the_radios_own_channels_when_none_survive():
    runner = _tri_band_runner("36 40", "36 40")

    OpenWrtStartupBootstrap(runner, config=_wifi_only()).prune_auto_channels(_tri_band_caps())

    batch = _batch_payloads(runner)[0]
    assert "radio1" not in batch
    assert "set wireless.radio2.channels='149 157 165'" in batch


def test_second_5ghz_radio_is_left_alone_without_a_second_5ghz_phy():
    # Only one 5 GHz phy is known: radio1 is checked, radio2 is not touched.
    runner = _tri_band_runner("36 149", "100 104")

    result = OpenWrtStartupBootstrap(runner, config=_wifi_only()).prune_auto_channels(_prune_caps())

    assert result.changed is False


def test_bootstrap_points_opennds_at_the_configured_interface():
    runner = BootstrapRunner(
        responses={
            ("uci", "-q", "get", "opennds.@opennds[0]"): CommandResult(
                returncode=0, stdout="opennds\n"
            ),
            ("ip", "-4", "route", "show", "default"): CommandResult(
                returncode=0, stdout="default via 192.0.2.1 dev br-lan\n"
            ),
        }
    )
    OpenWrtStartupBootstrap(
        runner, config=OpenWrtBootstrapConfig(ensure_lan=False, opennds_interface="br-guest")
    ).apply(_caps(has_opennds=True))

    assert "set opennds.@opennds[0].gatewayinterface='br-guest'" in _batch_payloads(runner)[0]


def test_bootstrap_does_not_put_opennds_on_the_uplink_bridge():
    runner = BootstrapRunner(
        responses={
            ("uci", "-q", "get", "opennds.@opennds[0]"): CommandResult(
                returncode=0, stdout="opennds\n"
            ),
            ("ip", "-4", "route", "show", "default"): CommandResult(
                returncode=0, stdout="default via 192.0.2.1 dev br-lan\n"
            ),
        }
    )
    result = OpenWrtStartupBootstrap(
        runner, config=OpenWrtBootstrapConfig(ensure_lan=False)
    ).apply(_caps(has_opennds=True))

    assert _batch_payloads(runner) == []
    assert (("/etc/init.d/opennds", "restart"), None) not in runner.calls
    # No warning at boot: an AP without a portal is normal. The portal runtime
    # reports the problem if a portal SSID arrives.
    assert result.warnings == ()


def test_bootstrap_leaves_opennds_off_without_a_default_route():
    # DHCP still waiting or the link down: the uplink is unknown, so openNDS
    # must not start on the LAN bridge and cut the AP off.
    runner = BootstrapRunner(
        responses={
            ("uci", "-q", "get", "opennds.@opennds[0]"): CommandResult(
                returncode=0, stdout="opennds\n"
            ),
            ("ip", "-4", "route", "show", "default"): CommandResult(returncode=0, stdout=""),
        }
    )
    OpenWrtStartupBootstrap(runner, config=OpenWrtBootstrapConfig(ensure_lan=False)).apply(
        _caps(has_opennds=True)
    )

    assert _batch_payloads(runner) == []
    assert (("/etc/init.d/opennds", "restart"), None) not in runner.calls


def test_bootstrap_uses_the_route_to_the_controller_without_a_default_route():
    # An AP on a network with no internet, with a routed guest bridge.
    runner = BootstrapRunner(
        responses={
            ("uci", "-q", "get", "opennds.@opennds[0]"): CommandResult(
                returncode=0, stdout="opennds\n"
            ),
            ("ip", "-4", "route", "show", "default"): CommandResult(returncode=0, stdout=""),
            ("ip", "-4", "route", "get", "192.0.2.10"): CommandResult(
                returncode=0, stdout="192.0.2.10 dev br-lan src 192.0.2.20 uid 0\n"
            ),
        }
    )
    OpenWrtStartupBootstrap(
        runner,
        config=OpenWrtBootstrapConfig(ensure_lan=False, opennds_interface="br-guest"),
        controller_host=lambda: "192.0.2.10",
    ).apply(_caps(has_opennds=True))

    assert "set opennds.@opennds[0].gatewayinterface='br-guest'" in _batch_payloads(runner)[0]
