"""Tools -> Network Check: run ping/traceroute/nslookup/ARP table on the AP.

The controller sends one SET key per tool (``ping``, ``traceroute``,
``dnslookup``, ``arptable``) with a ``cmdId``. The output goes back as
TROUBLESHOOTING NOTIFYs (``sub`` 5) whose content is
``{"cmdId": ..., "troubleshooting": {"msg", "bof", "seq", "finish"}}``: the
first chunk has ``bof=1``, the last ``finish=1``. ``msg`` is base64 (the
controller UI decodes it with ``atob``; plain text is dropped there).
"""
from __future__ import annotations

import base64
import logging
from pathlib import Path
import re
import subprocess
import threading
from typing import Any, Callable, Iterable, Sequence

from ....application.commands import ApplyDeviceConfigurationCommand
from ....application.contracts import PlatformCapabilities
from ....application.notifications import NOTIFY_SUBJECT_TROUBLESHOOTING
from ....contexts.device.domain import NetworkCheckRequest
from .uci import ReconciliationResult

log = logging.getLogger("open_omada.network_check")

RUN_TIMEOUT = 120.0
MAX_OUTPUT_LINES = 500
_DEST = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.:%-]{0,252}$")

Notify = Callable[[int, dict[str, Any]], None]
Popen = Callable[..., Any]


def network_check_argv(request: NetworkCheckRequest) -> list[str] | None:
    """Return the command for a request, or None for the ARP table (read from /proc)."""
    if request.kind == "arptable":
        return None
    if not request.dest or not _DEST.match(request.dest):
        raise ValueError(f"invalid {request.kind} destination")
    if request.kind == "ping":
        count = _clamp(request.count, 4, 1, 100)
        size = _clamp(request.packet_size, 56, 1, 65500)
        return ["ping", "-c", str(count), "-s", str(size), "-W", "2", request.dest]
    if request.kind == "traceroute":
        return ["traceroute", "-w", "2", "-m", "30", request.dest]
    if request.kind == "dnslookup":
        return ["nslookup", request.dest]
    raise ValueError(f"unknown network check {request.kind}")


def troubleshooting_content(cmd_id: str, msg: str, *, seq: int, bof: bool, finish: bool) -> dict[str, Any]:
    return {
        "cmdId": cmd_id,
        "troubleshooting": {
            "msg": base64.b64encode(msg.encode("utf-8")).decode("ascii"),
            "bof": int(bof),
            "seq": seq,
            "finish": int(finish),
        },
    }


class OpenWrtNetworkCheck:
    """Command port that runs each Network Check in a background thread."""

    def __init__(
        self,
        notify: Notify,
        *,
        popen: Popen = subprocess.Popen,
        arp_path: Path = Path("/proc/net/arp"),
        start_thread: Callable[[Callable[[], None]], None] | None = None,
        timeout: float = RUN_TIMEOUT,
    ) -> None:
        self._notify = notify
        self._popen = popen
        self._arp_path = arp_path
        self._start_thread = start_thread or _daemon_thread
        self._timeout = timeout

    def reconcile(
        self,
        update: ApplyDeviceConfigurationCommand,
        capabilities: PlatformCapabilities,
    ) -> ReconciliationResult:
        if not update.network_checks:
            return ReconciliationResult(applied=True, changed=False)
        if not capabilities.supports_network_check:
            return ReconciliationResult(
                applied=False, changed=False, error="Network Check is not supported on this AP"
            )
        for request in update.network_checks:
            try:
                argv = network_check_argv(request)
            except ValueError as exc:
                return ReconciliationResult(applied=False, changed=False, error=str(exc))
            log.info("Network Check %s started (cmdId=%s)", request.kind, request.cmd_id)
            self._start_thread(lambda request=request, argv=argv: self._run(request, argv))
        return ReconciliationResult(applied=True, changed=False)

    def _run(self, request: NetworkCheckRequest, argv: list[str] | None) -> None:
        stream = _ChunkStream(self._notify, request.cmd_id)
        try:
            if argv is None:
                stream.send_all(self._arp_lines())
            else:
                stream.send_all(self._command_lines(argv))
        except Exception as exc:  # noqa: BLE001 - report instead of killing the agent
            log.warning("Network Check %s failed: %s", request.kind, exc)
            stream.send(f"{request.kind} failed: {exc}\n")
        finally:
            stream.finish()
        log.info("Network Check %s finished (cmdId=%s)", request.kind, request.cmd_id)

    def _arp_lines(self) -> list[str]:
        return [line + "\n" for line in self._arp_path.read_text().splitlines()]

    def _command_lines(self, argv: Sequence[str]) -> Iterable[str]:
        process = self._popen(
            list(argv),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        timer = threading.Timer(self._timeout, process.kill)
        timer.daemon = True
        timer.start()
        try:
            for count, line in enumerate(process.stdout):
                if count >= MAX_OUTPUT_LINES:
                    process.kill()
                    break
                yield line if line.endswith("\n") else line + "\n"
            process.wait()
        finally:
            timer.cancel()


class _ChunkStream:
    """Send output lines as numbered chunks; the first has bof=1, the last finish=1."""

    def __init__(self, notify: Notify, cmd_id: str) -> None:
        self._notify = notify
        self._cmd_id = cmd_id
        self._seq = 0
        self._pending: str | None = None

    def send_all(self, lines: Iterable[str]) -> None:
        for line in lines:
            self.send(line)

    def send(self, line: str) -> None:
        # Hold one line back so the final chunk can carry finish=1.
        if self._pending is not None:
            self._emit(self._pending, finish=False)
        self._pending = line

    def finish(self) -> None:
        self._emit(self._pending or "", finish=True)
        self._pending = None

    def _emit(self, msg: str, *, finish: bool) -> None:
        self._notify(
            NOTIFY_SUBJECT_TROUBLESHOOTING,
            troubleshooting_content(
                self._cmd_id, msg, seq=self._seq, bof=self._seq == 0, finish=finish
            ),
        )
        self._seq += 1


def _clamp(value: int | None, default: int, low: int, high: int) -> int:
    if value is None or value <= 0:
        return default
    return max(low, min(high, value))


def _daemon_thread(target: Callable[[], None]) -> None:
    threading.Thread(target=target, name="network-check", daemon=True).start()
