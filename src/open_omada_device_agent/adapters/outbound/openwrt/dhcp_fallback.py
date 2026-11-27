"""DHCP fallback address for Device Config -> IP Settings.

An EAP in DHCP mode can use a fallback address when DHCP gets no lease, so it
stays reachable. OpenWrt's DHCP client (udhcpc run with ``-t 0``) retries
forever and never reports a failed lease, so the agent checks instead:

- the fallback is configured by ``ip_settings`` as ``network.<lan>``
  options ``openomada_fallback`` (``a.b.c.d/len``) and
  ``openomada_fallback_gateway``;
- after ``FALLBACK_DELAY`` seconds in DHCP mode without a lease, the address
  is added to the interface's device with ``ip``, plus a default route through
  the fallback gateway when there is none;
- as soon as DHCP gets a lease, or the fallback is no longer configured, both
  are removed again. DHCP keeps retrying in the meantime.

When the lease is in the same subnet, the lease address becomes a secondary
address behind the fallback. Linux deletes secondaries together with the
primary unless ``promote_secondaries`` is set, so the agent sets it on the
device first. The fallback default route uses metric ``ROUTE_METRIC`` so it
never replaces, or is removed in place of, the route DHCP installs.

netifd does not know about the address, so it is kept out of
``/etc/config/network`` and recorded in ``state_file`` for clean-up after an
agent restart.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .ip_settings import FALLBACK_GATEWAY_OPTION, FALLBACK_OPTION, parse_interface_show
from .uci import CommandRunner, SubprocessRunner

log = logging.getLogger("open_omada.ip_settings")

FALLBACK_DELAY = 30.0
CHECK_INTERVAL = 5.0
ROUTE_METRIC = "1000"


@dataclass(frozen=True)
class _Fallback:
    address: str  # a.b.c.d/len
    device: str
    gateway: str | None = None  # set only when the agent added the default route

    def dump(self) -> str:
        return " ".join((self.address, self.device, self.gateway or "-")) + "\n"

    @classmethod
    def load(cls, text: str) -> _Fallback | None:
        parts = text.split()
        if len(parts) != 3:
            return None
        return cls(parts[0], parts[1], None if parts[2] == "-" else parts[2])


class DhcpFallback:
    def __init__(
        self,
        *,
        interface: str = "lan",
        runner: CommandRunner | None = None,
        clock: Callable[[], float] = time.monotonic,
        state_file: Path = Path("/tmp/open-omada-ip-settings/fallback"),
        on_change: Callable[[], None] = lambda: None,
        delay: float = FALLBACK_DELAY,
        ipv4_conf: Path = Path("/proc/sys/net/ipv4/conf"),
    ) -> None:
        self._interface = interface
        self._runner = runner or SubprocessRunner()
        self._clock = clock
        self._state_file = state_file
        self._on_change = on_change
        self._delay = delay
        self._ipv4_conf = ipv4_conf
        self._without_lease_since: float | None = None

    def check(self) -> None:
        """Add or remove the fallback address; call it every few seconds."""
        shown = self._runner.run(["uci", "-q", "show", f"network.{self._interface}"])
        options = parse_interface_show(shown.stdout, self._interface) if shown.returncode == 0 else {}
        address = options.get(FALLBACK_OPTION) if options.get("proto") == "dhcp" else None
        gateway = options.get(FALLBACK_GATEWAY_OPTION) or None
        status = self._status()
        has_lease = bool(status.get("ipv4-address"))
        device = status.get("l3_device") or status.get("device")
        active = self._active()

        if not address or has_lease or not device:
            self._without_lease_since = None
            if active:
                self._remove(active)
                log.info(
                    "IP Settings: removed the fallback address %s (%s)",
                    active.address,
                    "DHCP lease received" if has_lease else "fallback no longer used",
                )
                self._on_change()
            return
        if active:
            if active.address == address and active.device == device:
                return
            self._remove(active)
        now = self._clock()
        if self._without_lease_since is None:
            self._without_lease_since = now
        if now - self._without_lease_since < self._delay:
            return
        error = self._add(address, device, gateway)
        if error is not None:
            # Try again after another delay instead of every check.
            self._without_lease_since = now
            log.warning("IP Settings: could not add the fallback address %s: %s", address, error)
            return
        log.warning(
            "IP Settings: no DHCP lease on %s after %.0fs; using the fallback address %s",
            self._interface,
            now - self._without_lease_since,
            address,
        )
        self._on_change()

    def _status(self) -> dict:
        result = self._runner.run(["ubus", "call", f"network.interface.{self._interface}", "status"])
        if result.returncode != 0:
            return {}
        try:
            status = json.loads(result.stdout)
        except ValueError:
            return {}
        return status if isinstance(status, dict) else {}

    def _active(self) -> _Fallback | None:
        try:
            return _Fallback.load(self._state_file.read_text())
        except OSError:
            return None

    def _keep_secondaries(self, device: str) -> None:
        try:
            (self._ipv4_conf / device / "promote_secondaries").write_text("1\n")
        except OSError as exc:
            log.warning("IP Settings: could not set promote_secondaries on %s: %s", device, exc)

    def _add(self, address: str, device: str, gateway: str | None) -> str | None:
        """Add the address (and route); the error, or None once it is in place."""
        self._keep_secondaries(device)
        added = self._runner.run(["ip", "-4", "addr", "add", address, "dev", device])
        if added.returncode != 0:
            return (added.stderr or added.stdout or f"ip exited with {added.returncode}").strip()
        route_added = None
        if gateway and not self._runner.run(["ip", "-4", "route", "show", "default"]).stdout.strip():
            added = self._runner.run(
                ["ip", "-4", "route", "add", "default", "via", gateway, "dev", device, "metric", ROUTE_METRIC]
            )
            if added.returncode == 0:
                route_added = gateway
        self._state_file.parent.mkdir(parents=True, exist_ok=True)
        self._state_file.write_text(_Fallback(address, device, route_added).dump())
        return None

    def _remove(self, active: _Fallback) -> None:
        self._keep_secondaries(active.device)  # the DHCP address may be a secondary
        if active.gateway:
            self._runner.run(
                ["ip", "-4", "route", "del", "default", "via", active.gateway, "dev", active.device,
                 "metric", ROUTE_METRIC]
            )
        self._runner.run(["ip", "-4", "addr", "del", active.address, "dev", active.device])
        self._state_file.unlink(missing_ok=True)


def start_dhcp_fallback(fallback: DhcpFallback, interval: float = CHECK_INTERVAL) -> threading.Thread:
    def run() -> None:
        while True:
            try:
                fallback.check()
            except Exception:  # noqa: BLE001 - never kill the agent over this
                log.exception("IP Settings: DHCP fallback check failed")
            time.sleep(interval)

    thread = threading.Thread(target=run, name="dhcp-fallback", daemon=True)
    thread.start()
    return thread
