"""Config -> Services -> LLDP: run lldpd on the LAN ports and report neighbours.

The controller sends SET key ``lldp`` with ``{"enable": 1|0}``; the per-AP
and site settings are already resolved. The agent writes these options of
``lldpd.config`` (OpenWrt ``lldpd`` package) and leaves the rest alone:

- ``enable_lldp`` on and CDP, FDP, SONMP and EDP off, as on an EAP;
- ``interface``: the wired ports of the LAN bridge. lldpd on the bridge
  itself would not hear neighbours, since bridges do not pass LLDP frames up.

lldpd sends the MAC of a LAN port as the chassis ID (subtype MAC, as an EAP
does). The bridge cannot be used for it: lldpd does not manage bridges and
falls back to the hostname, and ``configure system chassisid`` sends the
subtype "local". On a device whose ports have random MACs the chassis ID is
therefore not the device MAC.

lldpd is stopped and disabled when LLDP is off. While it runs, the inform
carries the neighbours from ``lldpcli -f json0 show neighbors`` under the
agent's LAN port name.
"""
from __future__ import annotations

import json
import logging
import re
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from ....application.commands import ApplyDeviceConfigurationCommand
from ....application.contracts import PlatformCapabilities
from .uci import CommandRunner, ReconciliationResult, SubprocessRunner

log = logging.getLogger("open_omada.lldp")

SECTION = "config"
_OFF_PROTOCOLS = ("enable_cdp", "enable_fdp", "enable_sonmp", "enable_edp")
_SHOW_LINE = re.compile(r"^lldpd\.config(?:\.([^=]+))?=(.*)$")
_QUOTED = re.compile(r"'((?:[^']|'\\'')*)'")
# Omada capability string: one '0'/'1' per LLDP capability, in this order.
CAPABILITY_ORDER = ("Station", "Docsis", "Telephone", "Router", "Wlan", "Bridge", "Repeater", "Other")
NEIGHBOUR_CACHE_SECONDS = 30.0
LLDPCLI_TIMEOUT = 5.0  # informs wait for this; a stuck lldpd must not stall them


def show_neighbours(timeout: float = LLDPCLI_TIMEOUT) -> str | None:
    """``lldpcli -f json0 show neighbors`` output, or None if lldpd does not answer."""
    try:
        completed = subprocess.run(
            ["lldpcli", "-f", "json0", "show", "neighbors"],
            text=True,
            capture_output=True,
            check=False,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return completed.stdout if completed.returncode == 0 else None


def wired_bridge_ports(bridge: str, sys_net: Path = Path("/sys/class/net")) -> tuple[str, ...]:
    """Ports of ``bridge`` that are not Wi-Fi interfaces, sorted."""
    try:
        ports = sorted(entry.name for entry in (sys_net / bridge / "brif").iterdir())
    except OSError:
        return ()
    return tuple(
        port
        for port in ports
        if not (sys_net / port / "phy80211").exists() and not (sys_net / port / "wireless").exists()
    )


def desired_options(interfaces: Sequence[str]) -> dict[str, str | tuple[str, ...]]:
    options: dict[str, str | tuple[str, ...]] = {"enable_lldp": "1"}
    options.update({name: "0" for name in _OFF_PROTOCOLS})
    options["interface"] = tuple(interfaces)
    return options


def parse_lldpd_show(text: str) -> dict[str, str | tuple[str, ...]] | None:
    """Options of ``lldpd.config`` from ``uci show lldpd``; None if it is missing."""
    options: dict[str, str | tuple[str, ...]] | None = None
    for line in text.splitlines():
        match = _SHOW_LINE.match(line.strip())
        if match is None:
            continue
        option, value = match.groups()
        if option is None:
            options = {} if options is None else options
            continue
        options = {} if options is None else options
        values = tuple(_unquote(item) for item in _QUOTED.findall(value))
        options[option] = values[0] if len(values) == 1 and option != "interface" else values
    return options


def build_lldp_batch(
    current: Mapping[str, str | tuple[str, ...]] | None,
    interfaces: Sequence[str],
) -> list[str]:
    """``uci batch`` lines that make ``lldpd.config`` run LLDP on ``interfaces``."""
    lines: list[str] = []
    if current is None:
        lines.append(f"set lldpd.{SECTION}=lldpd")
        current = {}
    for option, value in desired_options(interfaces).items():
        have = current.get(option)
        if isinstance(value, tuple):
            if tuple(have or ()) != value:
                if have is not None:
                    lines.append(f"delete lldpd.{SECTION}.{option}")
                lines.extend(f"add_list lldpd.{SECTION}.{option}={_quote(item)}" for item in value)
        elif have != value:
            lines.append(f"set lldpd.{SECTION}.{option}={_quote(value)}")
    return lines


def omada_chassis_id(id_type: str, value: str) -> str:
    """MAC chassis IDs in the AA-BB-CC-DD-EE-FF form the controller uses for devices."""
    if id_type == "mac" and re.fullmatch(r"[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}", value):
        return value.upper().replace(":", "-")
    return value


def capability_bits(capabilities: Sequence[Mapping[str, Any]]) -> str:
    enabled = {item.get("type") for item in capabilities if item.get("enabled") is True}
    return "".join("1" if name in enabled else "0" for name in CAPABILITY_ORDER)


def parse_neighbours(text: str) -> list[dict[str, str]]:
    """Neighbours from ``lldpcli -f json0 show neighbors``."""
    try:
        data = json.loads(text)
    except ValueError:
        return []
    neighbours = []
    for block in _items(data, "lldp"):
        for interface in _items(block, "interface"):
            chassis = _first(interface, "chassis")
            port = _first(interface, "port")
            chassis_id = _first(chassis, "id")
            port_id = _first(port, "id")
            if not chassis_id:
                continue
            neighbours.append(
                {
                    "chassisId": omada_chassis_id(
                        str(chassis_id.get("type", "")), str(chassis_id.get("value", ""))
                    ),
                    "portId": str(port_id.get("value", "")),
                    "name": _value(chassis, "name"),
                    "description": _value(chassis, "descr"),
                    "capabilities": capability_bits(_items(chassis, "capability")),
                }
            )
    return neighbours


class OpenWrtLldpAdapter:
    """Command port for SET key ``lldp`` and the inform's ``lldp`` neighbours."""

    def __init__(
        self,
        *,
        bridge: str = "br-lan",
        lan_port: str = "LAN",
        runner: CommandRunner | None = None,
        init_script: str = "/etc/init.d/lldpd",
        sys_net: Path = Path("/sys/class/net"),
        clock: Callable[[], float] = time.monotonic,
        neighbours: Callable[[], str | None] = show_neighbours,
    ) -> None:
        self._bridge = bridge
        self._lan_port = lan_port
        self._runner = runner or SubprocessRunner()
        self._init_script = init_script
        self._sys_net = sys_net
        self._clock = clock
        self._neighbours = neighbours
        self._cache: tuple[float, dict[str, Any] | None] | None = None

    def reconcile(
        self,
        update: ApplyDeviceConfigurationCommand,
        capabilities: PlatformCapabilities,
    ) -> ReconciliationResult:
        config = update.lldp
        if config is None:
            return ReconciliationResult(applied=True, changed=False)
        if not capabilities.supports_lldp:
            log.info("LLDP setting received but lldpd is not installed; not applied")
            return ReconciliationResult(applied=True, changed=False)
        self._cache = None
        if not config.enabled:
            changed = self._stop()
            log.info("LLDP disabled")
            return ReconciliationResult(applied=True, changed=changed)

        interfaces = wired_bridge_ports(self._bridge, self._sys_net) or (self._bridge,)
        shown = self._runner.run(["uci", "-q", "show", "lldpd"])
        batch = build_lldp_batch(parse_lldpd_show(shown.stdout), interfaces)
        if batch:
            result = self._runner.run(
                ["uci", "-q", "batch"], input_text="\n".join([*batch, "commit lldpd"]) + "\n"
            )
            if result.returncode != 0:
                self._runner.run(["uci", "revert", "lldpd"])
                return ReconciliationResult(
                    applied=False, changed=False, error="uci batch for lldpd failed"
                )
        changed = self._start(restart=bool(batch)) or bool(batch)
        log.info("LLDP enabled on %s", ",".join(interfaces))
        return ReconciliationResult(applied=True, changed=changed, command_count=len(batch))

    def inform_item(self) -> dict[str, Any] | None:
        """Inform value for key ``lldp``; None while lldpd is not running.

        Only answers are cached: right after a (re)start lldpd may not answer
        yet, and the next inform should try again.
        """
        now = self._clock()
        if self._cache is not None and now - self._cache[0] < NEIGHBOUR_CACHE_SECONDS:
            return self._cache[1]
        output = self._neighbours()
        if output is None:
            self._cache = None
            return None
        item = {"lldps": [{"port": self._lan_port, "neighbors": parse_neighbours(output)}]}
        self._cache = (now, item)
        return item

    def _start(self, *, restart: bool) -> bool:
        is_enabled = self._runner.run([self._init_script, "enabled"]).returncode == 0
        if not is_enabled:
            self._runner.run([self._init_script, "enable"])
        if restart or not is_enabled:
            self._runner.run([self._init_script, "restart"])
            return True
        # Enabled but maybe not running (stopped by hand); start is a no-op
        # for a running procd service.
        self._runner.run([self._init_script, "start"])
        return False

    def _stop(self) -> bool:
        # Stop even when not enabled at boot: lldpd may have been started by hand.
        is_enabled = self._runner.run([self._init_script, "enabled"]).returncode == 0
        self._runner.run([self._init_script, "stop"])
        if is_enabled:
            self._runner.run([self._init_script, "disable"])
        return is_enabled


def _items(data: Any, key: str) -> list[Mapping[str, Any]]:
    value = data.get(key) if isinstance(data, Mapping) else None
    return [item for item in value if isinstance(item, Mapping)] if isinstance(value, list) else []


def _first(data: Any, key: str) -> Mapping[str, Any]:
    items = _items(data, key)
    return items[0] if items else {}


def _value(data: Any, key: str) -> str:
    return str(_first(data, key).get("value", ""))


def _quote(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"


def _unquote(value: str) -> str:
    return value.replace("'\\''", "'")
