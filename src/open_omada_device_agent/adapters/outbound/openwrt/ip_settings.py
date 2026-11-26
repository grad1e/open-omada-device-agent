"""Device Config -> IP Settings: DHCP or a static LAN address on OpenWrt.

SET key ``lanSetting`` (connType 0 static, 1 DHCP) is applied to the UCI
``network.<lan>`` interface. A wrong address cuts the AP off from the
controller, so every change is guarded:

1. The SET is answered first; the network changes a moment later.
2. ``/etc/config/network`` is copied and a detached watchdog shell starts. It
   restores the copy and reloads the network unless the change is confirmed in
   time, even if the agent itself dies.
3. The agent confirms once it can open a TCP connection to the controller's
   manage port from the new address.
4. After a rollback the controller still holds the bad setting and may send it
   again on the next full sync. The watchdog records it, and the agent does not
   apply that same change again until the AP reboots.

In DHCP mode the controller's fallback address is stored in the interface
(``openomada_fallback``, ``openomada_fallback_gateway``) and added by
``dhcp_fallback.DhcpFallback`` while DHCP gets no lease.
"""
from __future__ import annotations

import hashlib
import ipaddress
import logging
import os
import re
import shutil
import socket
import subprocess
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ....application.commands import ApplyDeviceConfigurationCommand
from ....application.contracts import PlatformCapabilities
from ....contexts.networking.domain import LanIpSetting
from .uci import CommandRunner, ReconciliationResult, SubprocessRunner

log = logging.getLogger("open_omada.ip_settings")

APPLY_DELAY = 2.0  # let SET_RESPONSE reach the controller first
CONFIRM_TIMEOUT = 180.0
CONFIRM_INTERVAL = 5.0
ROLLBACK_SETTLE = 15.0  # network reload after the watchdog restores the config

FALLBACK_OPTION = "openomada_fallback"
FALLBACK_GATEWAY_OPTION = "openomada_fallback_gateway"

Endpoint = Callable[[], tuple[str, int] | None]
Connect = Callable[[str, int], bool]


def _address_error(kind: str, address: str | None, netmask: str | None, gateway: str | None) -> str | None:
    if not address or not netmask:
        return f"{kind} IP Settings need an IP address and a netmask"
    try:
        interface = ipaddress.IPv4Interface(f"{address}/{netmask}")
    except ValueError as exc:
        return f"invalid {kind} IP address or netmask: {exc}"
    network = interface.network
    if network.prefixlen < 31 and interface.ip in (network.network_address, network.broadcast_address):
        return f"{interface.ip} is the network or broadcast address of {network}"
    if gateway:
        try:
            gateway_ip = ipaddress.IPv4Address(gateway)
        except ValueError:
            return f"invalid {kind} gateway {gateway}"
        if gateway_ip not in network or gateway_ip == interface.ip:
            return f"{kind} gateway {gateway_ip} is not another address in {network}"
    return None


def validate_lan_setting(setting: LanIpSetting) -> str | None:
    if setting.mode == "dhcp":
        if not setting.fallback_enabled:
            return None
        return _address_error(
            "fallback", setting.fallback_ip, setting.fallback_netmask, setting.fallback_gateway
        )
    error = _address_error("static", setting.ip_address, setting.netmask, setting.gateway)
    if error:
        return error
    for server in setting.dns:
        try:
            ipaddress.ip_address(server)
        except ValueError:
            return f"invalid DNS server {server}"
    return None


def static_cidr(setting: LanIpSetting) -> str:
    interface = ipaddress.IPv4Interface(f"{setting.ip_address}/{setting.netmask}")
    return f"{interface.ip}/{interface.network.prefixlen}"


def fallback_cidr(setting: LanIpSetting) -> str | None:
    """The DHCP fallback address as ``a.b.c.d/len``, or None when it is off."""
    if setting.mode != "dhcp" or not setting.fallback_enabled:
        return None
    interface = ipaddress.IPv4Interface(f"{setting.fallback_ip}/{setting.fallback_netmask}")
    return f"{interface.ip}/{interface.network.prefixlen}"


def parse_interface_show(text: str, interface: str) -> dict[str, Any]:
    """Parse ``uci show network.<iface>``; list options become tuples."""
    options: dict[str, Any] = {}
    pattern = re.compile(rf"^network\.{re.escape(interface)}\.([^=]+)=(.*)$")
    for line in text.splitlines():
        match = pattern.match(line.strip())
        if match is None:
            continue
        name, raw = match.groups()
        values = re.findall(r"'((?:[^']|'\\'')*)'", raw) or [raw]
        values = [value.replace("'\\''", "'") for value in values]
        options[name] = values[0] if len(values) == 1 and name != "dns" else tuple(values)
    return options


def current_lan_setting(options: dict[str, Any]) -> LanIpSetting | None:
    """What the AP runs now, as Omada sees it (``None`` for other protocols)."""
    proto = options.get("proto")
    dns = tuple(options.get("dns", ()))
    if proto == "dhcp":
        try:
            fallback = ipaddress.IPv4Interface(options.get(FALLBACK_OPTION) or "")
        except ValueError:
            return LanIpSetting(mode="dhcp", dns=dns)
        return LanIpSetting(
            mode="dhcp",
            dns=dns,
            fallback_enabled=True,
            fallback_ip=str(fallback.ip),
            fallback_netmask=str(fallback.netmask),
            fallback_gateway=options.get(FALLBACK_GATEWAY_OPTION) or None,
        )
    if proto != "static":
        return None
    raw = options.get("ipaddr")
    raw = raw[0] if isinstance(raw, tuple) else raw
    if not raw:
        return None
    try:
        if "/" in raw:
            interface = ipaddress.IPv4Interface(raw)
        else:
            interface = ipaddress.IPv4Interface(f"{raw}/{options.get('netmask', '255.255.255.0')}")
    except ValueError:
        return None
    return LanIpSetting(
        mode="static",
        ip_address=str(interface.ip),
        netmask=str(interface.netmask),
        gateway=options.get("gateway") or None,
        dns=dns,
    )


def build_ip_batch(setting: LanIpSetting, options: dict[str, Any], interface: str) -> list[str]:
    """``uci batch`` lines that move ``network.<iface>`` to ``setting``."""
    section = f"network.{interface}"
    current = current_lan_setting(options)
    lines: list[str] = []
    fallback = fallback_cidr(setting)
    fallback_gateway = setting.fallback_gateway if fallback else None
    if setting.mode == "dhcp":
        if (
            current is not None
            and current.mode == "dhcp"
            and not any(key in options for key in ("ipaddr", "netmask", "gateway"))
            and options.get(FALLBACK_OPTION) == fallback
            and options.get(FALLBACK_GATEWAY_OPTION) == fallback_gateway
        ):
            return []
        lines.append(f"set {section}.proto='dhcp'")
        for option in ("ipaddr", "netmask", "gateway", "dns"):
            if option in options:
                lines.append(f"delete {section}.{option}")
        lines.extend(_set_or_delete(section, FALLBACK_OPTION, fallback, options))
        lines.extend(_set_or_delete(section, FALLBACK_GATEWAY_OPTION, fallback_gateway, options))
        return lines

    wanted = LanIpSetting(
        mode="static",
        ip_address=setting.ip_address,
        netmask=str(ipaddress.IPv4Interface(static_cidr(setting)).netmask),
        gateway=setting.gateway,
        dns=setting.dns,
    )
    stale = [o for o in ("netmask", FALLBACK_OPTION, FALLBACK_GATEWAY_OPTION) if o in options]
    if current == wanted and not stale:
        return []
    lines.append(f"set {section}.proto='static'")
    lines.append(f"set {section}.ipaddr='{static_cidr(setting)}'")
    if "netmask" in options:
        lines.append(f"delete {section}.netmask")
    if setting.gateway:
        lines.append(f"set {section}.gateway='{setting.gateway}'")
    elif "gateway" in options:
        lines.append(f"delete {section}.gateway")
    if "dns" in options:
        lines.append(f"delete {section}.dns")
    for server in setting.dns:
        lines.append(f"add_list {section}.dns='{server}'")
    for option in (FALLBACK_OPTION, FALLBACK_GATEWAY_OPTION):
        if option in options:
            lines.append(f"delete {section}.{option}")
    return lines


def _set_or_delete(section: str, option: str, value: str | None, options: dict[str, Any]) -> list[str]:
    if value:
        return [f"set {section}.{option}='{value}'"] if options.get(option) != value else []
    return [f"delete {section}.{option}"] if option in options else []


def lan_setting_report(setting: LanIpSetting | None) -> dict[str, Any] | None:
    """``lanSetting`` for DEVICE_NEGOTIATION (ReportedLanSetting)."""
    if setting is None:
        return None
    if setting.mode == "dhcp":
        if not setting.fallback_enabled:
            return {"connType": 1, "useFallBack": False}
        report = {
            "connType": 1,
            "useFallBack": True,
            "fallBackIp": setting.fallback_ip,
            "fallBackMask": setting.fallback_netmask,
        }
        if setting.fallback_gateway:
            report["fallbackGateway"] = setting.fallback_gateway
        return report
    report: dict[str, Any] = {
        "connType": 0,
        "ipAddr": setting.ip_address,
        "netMask": setting.netmask,
    }
    if setting.gateway:
        report["gateway"] = setting.gateway
    if setting.dns:
        report["preferredDNS"] = setting.dns[0]
    if len(setting.dns) > 1:
        report["alternateDNS"] = setting.dns[1]
    return report


class OpenWrtIpSettings:
    """Command port for SET key ``lanSetting``, plus the adoption report."""

    def __init__(
        self,
        *,
        interface: str = "lan",
        runner: CommandRunner | None = None,
        controller_endpoint: Endpoint = lambda: None,
        connect: Connect | None = None,
        schedule: Callable[[Callable[[], None]], None] | None = None,
        spawn: Callable[[str], None] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        network_config: Path = Path("/etc/config/network"),
        state_dir: Path = Path("/tmp/open-omada-ip-settings"),
        confirm_timeout: float = CONFIRM_TIMEOUT,
        on_address_changed: Callable[[], None] = lambda: None,
    ) -> None:
        self._interface = interface
        self._runner = runner or SubprocessRunner()
        self._endpoint = controller_endpoint
        self._connect = connect or _tcp_reachable
        self._schedule = schedule or _delayed_thread
        self._spawn = spawn or _spawn_detached
        self._sleep = sleep
        self._clock = clock
        self._network_config = network_config
        self._state_dir = state_dir
        self._confirm_timeout = confirm_timeout
        self._on_address_changed = on_address_changed
        self._busy = threading.Lock()  # held from scheduling until confirm or rollback

    def current(self) -> LanIpSetting | None:
        shown = self._runner.run(["uci", "-q", "show", f"network.{self._interface}"])
        if shown.returncode != 0:
            return None
        return current_lan_setting(parse_interface_show(shown.stdout, self._interface))

    def report(self) -> dict[str, Any] | None:
        return lan_setting_report(self.current())

    def reconcile(
        self,
        update: ApplyDeviceConfigurationCommand,
        capabilities: PlatformCapabilities,
    ) -> ReconciliationResult:
        setting = update.lan_ip
        if setting is None:
            return ReconciliationResult(applied=True, changed=False)
        if not capabilities.supports_ip_settings:
            log.info("IP Settings received; not applied (set OMADA_CAP_IP_SETTINGS=true to apply)")
            return ReconciliationResult(applied=True, changed=False)
        error = validate_lan_setting(setting)
        if error:
            return ReconciliationResult(applied=False, changed=False, error=error)
        shown = self._runner.run(["uci", "-q", "show", f"network.{self._interface}"])
        options = parse_interface_show(shown.stdout, self._interface)
        if not options:
            return ReconciliationResult(
                applied=False, changed=False, error=f"network.{self._interface} not found"
            )
        batch = build_ip_batch(setting, options, self._interface)
        if not batch:
            return ReconciliationResult(applied=True, changed=False)
        if self._rolled_back(batch):
            log.warning(
                "IP Settings: not applied; this change was rolled back earlier because the "
                "controller was unreachable. Correct the IP Settings in the controller."
            )
            return ReconciliationResult(applied=True, changed=False)
        log.info(
            "IP Settings: switching %s to %s%s in %.0fs (rollback after %.0fs without the controller)",
            self._interface,
            setting.mode,
            f" {static_cidr(setting)}" if setting.mode == "static" else "",
            APPLY_DELAY,
            self._confirm_timeout,
        )
        # One change at a time: a second apply started before the first is
        # confirmed would snapshot the new config as its rollback and undo the
        # first watchdog's rollback.
        if not self._busy.acquire(blocking=False):
            log.info("IP Settings: a change is still pending; this one is not applied")
            return ReconciliationResult(applied=True, changed=False)
        moved = _address_changes(current_lan_setting(options), setting)

        def job() -> None:
            try:
                self._apply(batch, moved=moved)
            finally:
                self._busy.release()

        try:
            self._schedule(job)
        except BaseException:
            self._busy.release()
            raise
        return ReconciliationResult(applied=True, changed=True, command_count=len(batch))

    @property
    def _rejected_file(self) -> Path:
        return self._state_dir / "rolled-back"

    def _rolled_back(self, batch: list[str]) -> bool:
        try:
            return self._rejected_file.read_text().strip() == _fingerprint(batch)
        except OSError:
            return False

    def _apply(self, batch: list[str], *, moved: bool = True) -> None:
        token = uuid.uuid4().hex[:12]
        self._state_dir.mkdir(parents=True, exist_ok=True)
        rollback = self._state_dir / f"network.{token}"
        confirm = self._state_dir / f"confirmed.{token}"
        shutil.copyfile(self._network_config, rollback)

        result = self._runner.run(
            ["uci", "-q", "batch"], input_text="\n".join([*batch, "commit network"]) + "\n"
        )
        if result.returncode != 0:
            self._runner.run(["uci", "revert", "network"])
            log.warning("IP Settings: uci batch failed; nothing changed")
            rollback.unlink(missing_ok=True)
            return

        self._spawn(_watchdog_script(
            rollback=rollback,
            confirm=confirm,
            network_config=self._network_config,
            timeout=self._confirm_timeout,
            rejected=self._rejected_file,
            fingerprint=_fingerprint(batch),
        ))
        started = self._clock()
        self._runner.run(["/etc/init.d/network", "reload"])
        if moved:
            # The controller session is bound to the old address.
            self._on_address_changed()

        deadline = started + self._confirm_timeout - CONFIRM_INTERVAL
        while self._clock() < deadline:
            self._sleep(CONFIRM_INTERVAL)
            endpoint = self._endpoint()
            if endpoint and self._connect(*endpoint):
                confirm.touch()
                log.info("IP Settings: controller reachable at %s:%d; change kept", *endpoint)
                return
        log.warning("IP Settings: controller unreachable; the watchdog restores the previous network config")
        if moved:
            # Reconnect again once the watchdog has put the old address back.
            remaining = started + self._confirm_timeout + ROLLBACK_SETTLE - self._clock()
            if remaining > 0:
                self._sleep(remaining)
            self._on_address_changed()


def _address_changes(current: LanIpSetting | None, wanted: LanIpSetting) -> bool:
    if current is None or current.mode != wanted.mode:
        return True
    if wanted.mode == "dhcp":
        return False
    return static_cidr(current) != static_cidr(wanted)


def _fingerprint(batch: list[str]) -> str:
    return hashlib.sha256("\n".join(batch).encode()).hexdigest()[:16]


def _watchdog_script(
    *,
    rollback: Path,
    confirm: Path,
    network_config: Path,
    timeout: float,
    rejected: Path,
    fingerprint: str,
) -> str:
    # The change is recorded as rolled back (and the copy removed) only when
    # the old config was really put back; otherwise the copy is kept for a
    # manual restore and the agent may retry the change.
    return (
        f"sleep {int(timeout)}; "
        f"if [ -f '{confirm}' ]; then rm -f '{confirm}' '{rollback}' '{rejected}'; exit 0; fi; "
        f"if cp '{rollback}' '{network_config}'; then "
        "/etc/init.d/network reload; "
        f"rm -f '{rollback}'; echo {fingerprint} > '{rejected}'; "
        "logger -t open-omada 'IP Settings rolled back: controller unreachable'; "
        "else "
        f"logger -t open-omada 'IP Settings rollback FAILED: restore {network_config} from {rollback} by hand'; "
        "fi"
    )


def _tcp_reachable(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=3):
            return True
    except OSError:
        return False


def _delayed_thread(target: Callable[[], None]) -> None:
    def run() -> None:
        time.sleep(APPLY_DELAY)
        try:
            target()
        except Exception:  # noqa: BLE001 - never kill the agent over this
            log.exception("IP Settings apply failed")

    threading.Thread(target=run, name="ip-settings", daemon=True).start()


def _spawn_detached(script: str) -> None:
    subprocess.Popen(
        ["/bin/sh", "-c", script],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
        env={"PATH": os.environ.get("PATH", "/usr/sbin:/usr/bin:/sbin:/bin")},
    )
