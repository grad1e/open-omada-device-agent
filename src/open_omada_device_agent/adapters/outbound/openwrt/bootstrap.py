"""OpenWrt startup bootstrap for AP runtime prerequisites."""
from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass

from ....application.contracts import PlatformCapabilities
from .opennds import uplink_capture_message, uplink_device
from .uci import CommandRunner, SubprocessRunner, assign_radio_bands, uci_quote, usable_channels


log = logging.getLogger("open_omada.openwrt.bootstrap")


@dataclass(frozen=True)
class OpenWrtBootstrapConfig:
    enabled: bool = True
    ensure_lan: bool = True
    lan_interface: str = "lan"
    lan_bridge: str = "br-lan"
    lan_ipaddr: str = "192.168.1.1/24"
    ensure_opennds: bool = True
    opennds_gateway_port: int = 2050
    opennds_gateway_name: str = "OpenOmada-AP"
    enable_wan_management: bool = False
    wan_zone: str = "wan"
    wifi_country: str = ""
    opennds_interface: str = ""


@dataclass(frozen=True)
class OpenWrtBootstrapResult:
    applied: bool
    changed: bool = False
    warnings: tuple[str, ...] = ()
    command_count: int = 0
    wireless_changed: bool = False


# How long to wait for the kernel to apply a new Wi-Fi country after
# `wifi reload`; capabilities read earlier still show the old channel list.
REGDOMAIN_WAIT_SECONDS = 10.0
REGDOMAIN_POLL_SECONDS = 0.5


class OpenWrtStartupBootstrap:
    def __init__(
        self,
        runner: CommandRunner | None = None,
        *,
        config: OpenWrtBootstrapConfig | None = None,
        sleep: Callable[[float], None] = time.sleep,
        controller_host: Callable[[], str | None] = lambda: None,
    ) -> None:
        self._runner = runner or SubprocessRunner()
        self._config = config or OpenWrtBootstrapConfig()
        self._controller_host = controller_host
        self._sleep = sleep

    def apply(self, capabilities: PlatformCapabilities) -> OpenWrtBootstrapResult:
        if not self._config.enabled or capabilities.platform != "openwrt":
            return OpenWrtBootstrapResult(applied=True)
        if not capabilities.has_uci:
            return OpenWrtBootstrapResult(
                applied=True,
                warnings=("OpenWrt bootstrap skipped because uci is not available",),
            )

        changed = False
        command_count = 0
        warnings: list[str] = []

        if self._config.ensure_lan:
            result = self._ensure_lan_bridge()
            changed = changed or result.changed
            command_count += result.command_count
            warnings.extend(result.warnings)

        if self._config.enable_wan_management:
            result = self._ensure_wan_management()
            changed = changed or result.changed
            command_count += result.command_count
            warnings.extend(result.warnings)

        wifi = self._ensure_wifi_country()
        changed = changed or wifi.changed
        command_count += wifi.command_count
        warnings.extend(wifi.warnings)

        if self._config.ensure_opennds and capabilities.has_opennds:
            result = self._ensure_opennds()
            changed = changed or result.changed
            command_count += result.command_count
            warnings.extend(result.warnings)

        for warning in warnings:
            log.warning(warning)
        if changed:
            log.info("OpenWrt startup bootstrap applied %d command(s)", command_count)
        return OpenWrtBootstrapResult(
            applied=True,
            changed=changed,
            warnings=tuple(dict.fromkeys(warnings)),
            command_count=command_count,
            wireless_changed=wifi.changed,
        )

    def _ensure_wifi_country(self) -> OpenWrtBootstrapResult:
        radios = self._sections_of_type("wireless", "wifi-device")
        country = self._config.wifi_country
        if not country:
            missing = [r for r in radios if not self._uci_get(f"wireless.{r}.country")]
            if not missing:
                return OpenWrtBootstrapResult(applied=True)
            return OpenWrtBootstrapResult(
                applied=True,
                warnings=(
                    f"Wi-Fi radio(s) {', '.join(missing)} have no country code; the "
                    "controller does not send one, so channels it picks may be refused "
                    "and the radio may go down. Set OMADA_WIFI_COUNTRY (e.g. IN, US).",
                ),
            )
        lines: list[str] = []
        updated: list[str] = []
        for radio in radios:
            before = len(lines)
            self._append_set_if_needed(lines, "wireless", radio, "country", country)
            if len(lines) > before:
                updated.append(radio)
        if not lines:
            return OpenWrtBootstrapResult(applied=True)
        result = self._run_batch("wireless", lines)
        if not result.changed:
            return result
        log.info("Set Wi-Fi country %s on %s", country, ", ".join(updated))
        reload = self._runner.run(("wifi", "reload"))
        warnings = () if reload.returncode == 0 else (_command_warning(("wifi", "reload"), reload),)
        if reload.returncode == 0 and not self._wait_for_regdomain(country):
            warnings += (
                f"Kernel regulatory domain did not switch to {country} within "
                f"{REGDOMAIN_WAIT_SECONDS:.0f} s; advertised channels may be stale "
                "until the agent restarts.",
            )
        return OpenWrtBootstrapResult(
            applied=True,
            changed=True,
            warnings=warnings,
            command_count=result.command_count + 1,
        )

    def _wait_for_regdomain(self, country: str) -> bool:
        """Wait until ``iw reg get`` reports ``country``.

        Returns False only when iw keeps reporting another country; if iw is
        missing or prints no country, there is nothing to wait for.
        """
        expected = re.compile(rf"^country {re.escape(country)}:", re.MULTILINE)
        any_country = re.compile(r"^country \S+:", re.MULTILINE)
        for _ in range(int(REGDOMAIN_WAIT_SECONDS / REGDOMAIN_POLL_SECONDS)):
            result = self._runner.run(("iw", "reg", "get"))
            if result.returncode != 0 or not any_country.search(result.stdout or ""):
                return True
            if expected.search(result.stdout):
                return True
            self._sleep(REGDOMAIN_POLL_SECONDS)
        return False

    def prune_auto_channels(self, capabilities: PlatformCapabilities) -> OpenWrtBootstrapResult:
        """Drop channels the radio can no longer use from each Auto channel list.

        ``wireless.radioX.channels`` limits OpenWrt ACS. A list saved under
        another country or DFS state can make ACS pick a channel hostapd
        refuses (e.g. a radar channel under DFS-UNSET), taking the radio down.
        """
        if not self._config.enabled or capabilities.platform != "openwrt" or not capabilities.has_uci:
            return OpenWrtBootstrapResult(applied=True)
        lines: list[str] = []
        pruned: list[str] = []
        sections = self._sections_of_type("wireless", "wifi-device")
        # Same band rule as the settings side: a second 5g radio (by name) is
        # 5 GHz-2, checked against the second 5 GHz phy from ``iw list``, not
        # the first one's channels (Inferred: UCI name order follows phy
        # order; no tri-band board tested). Without that phy it is skipped.
        bands = {
            name: band
            for band, name in assign_radio_bands(
                {radio: self._uci_get(f"wireless.{radio}.band") for radio in sections}
            ).items()
        }
        for radio in sections:
            band = bands.get(radio)
            if band is None:
                continue
            current = self._uci_get(f"wireless.{radio}.channels").split()
            usable = usable_channels(capabilities, band)
            if not current or not usable:
                continue
            kept = [ch for ch in current if ch.isdigit() and int(ch) in usable]
            if kept == current:
                continue
            value = " ".join(kept) if kept else " ".join(map(str, usable))
            lines.append(f"set wireless.{radio}.channels='{uci_quote(value)}'")
            pruned.append(f"{radio} -> {value}")
        if not lines:
            return OpenWrtBootstrapResult(applied=True)
        result = self._run_batch("wireless", lines)
        if not result.changed:
            return result
        log.info("Removed unusable Auto channels: %s", "; ".join(pruned))
        reload = self._runner.run(("wifi", "reload"))
        warnings = () if reload.returncode == 0 else (_command_warning(("wifi", "reload"), reload),)
        return OpenWrtBootstrapResult(
            applied=True,
            changed=True,
            warnings=warnings,
            command_count=result.command_count + 1,
        )

    def _sections_of_type(self, config: str, section_type: str) -> list[str]:
        result = self._runner.run(("uci", "-q", "show", config))
        if result.returncode != 0:
            return []
        suffix = f"={section_type}"
        return [
            line[len(config) + 1 : -len(suffix)]
            for line in result.stdout.splitlines()
            if line.startswith(f"{config}.") and line.endswith(suffix) and line.count(".") == 1
        ]

    def _ensure_lan_bridge(self) -> OpenWrtBootstrapResult:
        bridge = self._config.lan_bridge
        lan = self._config.lan_interface
        lines: list[str] = []

        bridge_section = self._find_section_by_option("network", "name", bridge)
        if bridge_section is None:
            bridge_section = "openomada_br_lan"
            self._append_section_if_needed(lines, "network", bridge_section, "device")
            self._append_set_if_needed(lines, "network", bridge_section, "name", bridge)
        self._append_set_if_needed(lines, "network", bridge_section, "type", "bridge")
        self._append_set_if_needed(lines, "network", bridge_section, "bridge_empty", "1")

        if self._uci_get(f"network.{lan}") != "interface":
            self._append_section_if_needed(lines, "network", lan, "interface")
        if not self._uci_get(f"network.{lan}.device"):
            self._append_set_if_needed(lines, "network", lan, "device", bridge)
        if not self._uci_get(f"network.{lan}.proto"):
            self._append_set_if_needed(lines, "network", lan, "proto", "static")
        if not self._uci_get(f"network.{lan}.ipaddr"):
            self._append_set_if_needed(lines, "network", lan, "ipaddr", self._config.lan_ipaddr)

        if not lines:
            return OpenWrtBootstrapResult(applied=True)

        result = self._run_batch("network", lines)
        warnings = list(result.warnings)
        command_count = result.command_count
        if not result.changed:
            return result
        for command in (("/etc/init.d/network", "reload"), ("wifi", "reload")):
            run_result = self._runner.run(command)
            command_count += 1
            if run_result.returncode != 0:
                warnings.append(_command_warning(command, run_result))
        return OpenWrtBootstrapResult(
            applied=True,
            changed=True,
            warnings=tuple(warnings),
            command_count=command_count,
        )

    def _ensure_wan_management(self) -> OpenWrtBootstrapResult:
        lines: list[str] = []
        for section, name, port in (
            ("openomada_allow_ssh_wan", "Allow-SSH-WAN", "22"),
            ("openomada_allow_luci_http_wan", "Allow-LuCI-HTTP-WAN", "80"),
            ("openomada_allow_luci_https_wan", "Allow-LuCI-HTTPS-WAN", "443"),
        ):
            self._append_section_if_needed(lines, "firewall", section, "rule")
            self._append_set_if_needed(lines, "firewall", section, "name", name)
            self._append_set_if_needed(lines, "firewall", section, "src", self._config.wan_zone)
            self._append_set_if_needed(lines, "firewall", section, "proto", "tcp")
            self._append_set_if_needed(lines, "firewall", section, "dest_port", port)
            self._append_set_if_needed(lines, "firewall", section, "target", "ACCEPT")

        if not lines:
            return OpenWrtBootstrapResult(applied=True)

        result = self._run_batch("firewall", lines)
        warnings = list(result.warnings)
        command_count = result.command_count
        if not result.changed:
            return result
        run_result = self._runner.run(("/etc/init.d/firewall", "reload"))
        command_count += 1
        if run_result.returncode != 0:
            warnings.append(_command_warning(("/etc/init.d/firewall", "reload"), run_result))
        return OpenWrtBootstrapResult(
            applied=True,
            changed=True,
            warnings=tuple(warnings),
            command_count=command_count,
        )

    def _ensure_opennds(self) -> OpenWrtBootstrapResult:
        interface = self._config.opennds_interface or self._config.lan_bridge
        uplink = uplink_device(self._runner, self._controller_host())
        if uplink is None or interface == uplink:
            # A dumb AP without a guest network: leave openNDS off. Without a
            # default route or a route to a known controller (DHCP still
            # waiting, link down) the uplink is unknown, so openNDS stays off too. The portal runtime reports
            # this if the controller ever sends a portal SSID.
            log.debug("openNDS not started: %s", uplink_capture_message(interface, uplink))
            return OpenWrtBootstrapResult(applied=True)
        lines: list[str] = []
        section = "@opennds[0]"
        if self._uci_get("opennds.@opennds[0]") != "opennds":
            section = "openomada"
            self._append_section_if_needed(lines, "opennds", section, "opennds")

        self._append_set_if_needed(lines, "opennds", section, "enabled", "1")
        self._append_set_if_needed(
            lines,
            "opennds",
            section,
            "gatewayinterface",
            interface,
        )
        self._append_set_if_needed(
            lines,
            "opennds",
            section,
            "gatewayport",
            str(self._config.opennds_gateway_port),
        )
        if self._config.opennds_gateway_name:
            self._append_set_if_needed(
                lines,
                "opennds",
                section,
                "gatewayname",
                self._config.opennds_gateway_name,
            )
        self._append_set_if_needed(
            lines,
            "opennds",
            section,
            "gatewayfqdn",
            "disable",
        )
        self._append_set_if_needed(
            lines,
            "opennds",
            section,
            "allow_preemptive_authentication",
            "0",
        )

        command_count = 0
        warnings: list[str] = []
        changed = bool(lines)
        if lines:
            result = self._run_batch("opennds", lines)
            command_count += result.command_count
            warnings.extend(result.warnings)
            if not result.changed:
                return OpenWrtBootstrapResult(
                    applied=True,
                    warnings=tuple(warnings),
                    command_count=command_count,
                )

        action = "restart" if changed else "start"
        command = ("/etc/init.d/opennds", action)
        run_result = self._runner.run(command)
        command_count += 1
        if run_result.returncode != 0:
            warnings.append(_command_warning(command, run_result))
        return OpenWrtBootstrapResult(
            applied=True,
            changed=changed,
            warnings=tuple(warnings),
            command_count=command_count,
        )

    def _run_batch(self, config: str, lines: list[str]) -> OpenWrtBootstrapResult:
        payload = "\n".join((*lines, f"commit {config}")) + "\n"
        result = self._runner.run(("uci", "-q", "batch"), input_text=payload)
        if result.returncode != 0:
            return OpenWrtBootstrapResult(
                applied=True,
                warnings=(_command_warning(("uci", "-q", "batch"), result),),
                command_count=1,
            )
        return OpenWrtBootstrapResult(applied=True, changed=True, command_count=1)

    def _append_section_if_needed(
        self,
        lines: list[str],
        config: str,
        section: str,
        section_type: str,
    ) -> None:
        if self._uci_get(f"{config}.{section}") == section_type:
            return
        lines.append(f"set {config}.{section}={section_type}")

    def _append_set_if_needed(
        self,
        lines: list[str],
        config: str,
        section: str,
        option: str,
        value: str,
    ) -> None:
        if self._uci_get(f"{config}.{section}.{option}") == value:
            return
        lines.append(f"set {config}.{section}.{option}='{uci_quote(value)}'")

    def _uci_get(self, path: str) -> str:
        result = self._runner.run(("uci", "-q", "get", path))
        if result.returncode != 0:
            return ""
        return result.stdout.strip()

    def _find_section_by_option(self, config: str, option: str, value: str) -> str | None:
        result = self._runner.run(("uci", "-q", "show", config))
        if result.returncode != 0:
            return None
        suffix = f".{option}='{value}'"
        for line in result.stdout.splitlines():
            if not line.startswith(f"{config}.") or not line.endswith(suffix):
                continue
            return line[len(config) + 1 : -len(suffix)]
        return None


def _command_warning(command: tuple[str, ...], result) -> str:
    output = (result.stderr or result.stdout or "").strip()
    if output:
        return f"{' '.join(command)} failed: {output}"
    return f"{' '.join(command)} failed with exit code {result.returncode}"
