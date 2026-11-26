"""Nearby-AP scan for the controller's Interference Detection / Rogue AP views.

The controller starts a scan with the SET key ``rogueApScan`` and reads the
results from the inform key ``rogueApList`` (``RogueApStats`` in Controller
6.3.0.111: mac, ssid, rid, ch, sec, bcn, signal, pm). The scan runs in a
background thread because ``iw ... scan`` takes seconds per radio and the
managed session must keep answering the controller meanwhile.

The results go into the next inform only. The controller ends the scan when
it reads them; a repeated list logs "monitorScanStatusImageEnum is null"
(Validated: lab, Controller 6.3.0.111).
"""
from __future__ import annotations

import logging
import re
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from ....application.contracts import PlatformCapabilities
from ....application.commands import ApplyDeviceConfigurationCommand
from ....contexts.wireless.domain import RadioBand
from ....util.mac_format import to_omada
from .device_profile import RADIO_ID_BY_BAND
from .uci import CommandRunner, ReconciliationResult, SubprocessRunner

log = logging.getLogger("open_omada.rogue_ap")

# Controller 6.3.0.111 ``RogueApStats.phymode`` (monitor rogueap enum).
PM_11A, PM_11B, PM_11G, PM_11NA, PM_11NG, PM_11AC, PM_11AXA, PM_11AXG = range(8)

# Controller 6.3.0.111 ``RogueApStats.securityMode`` (monitor rogueap enum).
SEC_NONE = 0
SEC_WEP = 1
SEC_WPA_ENTERPRISE = 2
SEC_WPA_PSK = 3
SEC_WPA3_SAE = 4
SEC_OWE = 5
SEC_WPA3_ENTERPRISE = 6
SEC_WPA2_PSK_WPA3_SAE = 7
SEC_WPA_WPA2_PSK = 8
SEC_WPA_WPA2_ENTERPRISE = 9
SEC_WPA2_PSK = 10
SEC_WPA2_ENTERPRISE = 11
SEC_UNKNOWN = 15


@dataclass(frozen=True)
class ApInterface:
    ifname: str
    band: RadioBand
    mac: str


@dataclass(frozen=True)
class NeighbourAp:
    bssid: str
    ssid: str
    band: RadioBand
    channel: int
    security: int
    beacon_interval: int | None
    signal: int | None
    phy_mode: int

    def to_inform(self) -> dict[str, Any]:
        item: dict[str, Any] = {
            "mac": to_omada(self.bssid),
            "ssid": self.ssid,
            "rid": RADIO_ID_BY_BAND[self.band],
            "ch": self.channel,
            "sec": self.security,
            "pm": self.phy_mode,
        }
        if self.beacon_interval is not None:
            item["bcn"] = self.beacon_interval
        if self.signal is not None:
            item["signal"] = self.signal
        return item


def band_for_frequency(freq: float) -> RadioBand | None:
    if 2400 <= freq < 2500:
        return RadioBand.TWO_G
    if 5150 <= freq < 5925:
        return RadioBand.FIVE_G
    if 5925 <= freq < 7125:
        return RadioBand.SIX_G
    return None


def channel_for_frequency(freq: float) -> int | None:
    mhz = int(round(freq))
    if mhz == 2484:
        return 14
    if 2412 <= mhz <= 2472:
        return (mhz - 2407) // 5
    if 5150 <= mhz < 5925:
        return (mhz - 5000) // 5
    if 5955 <= mhz < 7125:
        return (mhz - 5950) // 5
    return None


_DEV_INTERFACE = re.compile(r"^\s*Interface\s+(\S+)")
_DEV_ADDR = re.compile(r"^\s*addr\s+([0-9a-fA-F:]{17})")
_DEV_TYPE = re.compile(r"^\s*type\s+(\S+)")
_DEV_CHANNEL = re.compile(r"^\s*channel\s+\d+\s+\((\d+)\s*MHz\)")


def parse_ap_interfaces(iw_dev_output: str) -> tuple[ApInterface, ...]:
    """Return the AP interfaces from ``iw dev``, one scan interface per band."""
    found: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    for line in iw_dev_output.splitlines():
        if match := _DEV_INTERFACE.match(line):
            current = {"ifname": match.group(1)}
            found.append(current)
        elif current is None:
            continue
        elif match := _DEV_ADDR.match(line):
            current["mac"] = match.group(1).lower()
        elif match := _DEV_TYPE.match(line):
            current["type"] = match.group(1)
        elif match := _DEV_CHANNEL.match(line):
            current["freq"] = match.group(1)
    interfaces: list[ApInterface] = []
    for item in found:
        if item.get("type") != "AP" or "freq" not in item:
            continue
        band = band_for_frequency(float(item["freq"]))
        if band is None:
            continue
        interfaces.append(ApInterface(item["ifname"], band, item.get("mac", "")))
    return tuple(interfaces)


_BSS = re.compile(r"^BSS\s+([0-9a-fA-F:]{17})")


def parse_scan(output: str, band: RadioBand) -> tuple[NeighbourAp, ...]:
    """Parse ``iw dev <ap> scan`` output into neighbour APs."""
    blocks: list[list[str]] = []
    for line in output.splitlines():
        if _BSS.match(line):
            blocks.append([line])
        elif blocks:
            blocks[-1].append(line)
    neighbours = []
    for block in blocks:
        neighbour = _parse_bss(block, band)
        if neighbour is not None:
            neighbours.append(neighbour)
    return tuple(neighbours)


def _parse_bss(lines: list[str], band: RadioBand) -> NeighbourAp | None:
    match = _BSS.match(lines[0])
    if match is None:
        return None
    bssid = match.group(1).lower()
    text = "\n".join(lines)
    freq = _float(r"^\tfreq:\s*([\d.]+)", text)
    channel = _int(r"\* primary channel:\s*(\d+)", text)
    if channel is None:
        channel = _int(r"DS Parameter set: channel\s+(\d+)", text)
    if channel is None and freq is not None:
        channel = channel_for_frequency(freq)
    if freq is not None:
        band = band_for_frequency(freq) or band
    if channel is None:
        return None
    ssid_match = re.search(r"^\tSSID:[ ]?([^\n]*)$", text, re.M)
    signal = _float(r"^\tsignal:\s*(-?[\d.]+)", text)
    return NeighbourAp(
        bssid=bssid,
        ssid=ssid_match.group(1) if ssid_match else "",
        band=band,
        channel=channel,
        security=_security(text),
        beacon_interval=_int(r"^\tbeacon interval:\s*(\d+)", text),
        signal=int(round(signal)) if signal is not None else None,
        phy_mode=_phy_mode(text, band),
    )


def _security(text: str) -> int:
    rsn = _element(text, "RSN")
    wpa = _element(text, "WPA")
    if rsn is None and wpa is None:
        # iw-tiny prints only "ESS (0x0411)", so read the Privacy bit (0x0010).
        capability = re.search(r"^\tcapability:.*?\(0x([0-9a-fA-F]+)\)", text, re.M)
        if capability and int(capability.group(1), 16) & 0x0010:
            return SEC_WEP
        return SEC_NONE
    rsn_auth = _auth_suites(rsn)
    wpa_auth = _auth_suites(wpa)
    if "OWE" in rsn_auth:
        return SEC_OWE
    if "SAE" in rsn_auth:
        return SEC_WPA2_PSK_WPA3_SAE if "PSK" in rsn_auth else SEC_WPA3_SAE
    if "SUITE-B" in rsn_auth:
        return SEC_WPA3_ENTERPRISE
    enterprise = "802.1X"
    if rsn is not None and wpa is not None:
        if enterprise in rsn_auth or enterprise in wpa_auth:
            return SEC_WPA_WPA2_ENTERPRISE
        return SEC_WPA_WPA2_PSK
    if rsn is not None:
        if enterprise in rsn_auth:
            return SEC_WPA2_ENTERPRISE
        return SEC_WPA2_PSK if "PSK" in rsn_auth else SEC_UNKNOWN
    if enterprise in wpa_auth:
        return SEC_WPA_ENTERPRISE
    return SEC_WPA_PSK if "PSK" in wpa_auth else SEC_UNKNOWN


def _element(text: str, name: str) -> str | None:
    """Return the indented body of an information element such as ``RSN:``."""
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if re.match(rf"^\t{name}:", line):
            body = [line]
            for following in lines[index + 1:]:
                if not following.startswith("\t\t"):
                    break
                body.append(following)
            return "\n".join(body)
    return None


def _auth_suites(element: str | None) -> str:
    if element is None:
        return ""
    match = re.search(r"Authentication suites:\s*(.*)", element)
    return match.group(1) if match else ""


def _phy_mode(text: str, band: RadioBand) -> int:
    he = "HE capabilities:" in text
    vht = "VHT capabilities:" in text
    ht = "HT capabilities:" in text
    if band is RadioBand.TWO_G:
        if he:
            return PM_11AXG
        if ht:
            return PM_11NG
        rates = " ".join(re.findall(r"rates:\s*(.*)", text))
        return PM_11G if re.search(r"\b(6|9|12|18|24|36|48|54)\.0", rates) else PM_11B
    if he or band is RadioBand.SIX_G:
        return PM_11AXA
    if vht:
        return PM_11AC
    if ht:
        return PM_11NA
    return PM_11A


def _int(pattern: str, text: str) -> int | None:
    match = re.search(pattern, text, re.M)
    return int(match.group(1)) if match else None


def _float(pattern: str, text: str) -> float | None:
    match = re.search(pattern, text, re.M)
    return float(match.group(1)) if match else None


@dataclass
class _ScanState:
    running: bool = False
    # Results not yet sent; None once the next inform has taken them.
    pending: tuple[NeighbourAp, ...] | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)


class OpenWrtRogueApScanner:
    """Command port for ``rogueApScan`` and source of the inform ``rogueApList``."""

    def __init__(
        self,
        runner: CommandRunner | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        start_thread: Callable[[Callable[[], None]], None] | None = None,
    ) -> None:
        self._runner = runner or SubprocessRunner()
        self._clock = clock
        self._start_thread = start_thread or _daemon_thread
        self._state = _ScanState()

    def reconcile(
        self,
        update: ApplyDeviceConfigurationCommand,
        capabilities: PlatformCapabilities,
    ) -> ReconciliationResult:
        if not update.rogue_ap_scan:
            return ReconciliationResult(applied=True, changed=False)
        if not capabilities.supports_rogue_ap_scan:
            return ReconciliationResult(
                applied=False,
                changed=False,
                error="rogueApScan needs iw on an OpenWrt AP",
            )
        with self._state.lock:
            if self._state.running:
                log.info("Rogue AP scan already running; ignoring the new request")
                return ReconciliationResult(applied=True, changed=False)
            self._state.running = True
        self._start_thread(self._scan)
        return ReconciliationResult(applied=True, changed=True)

    def inform_items(self) -> list[dict[str, Any]]:
        """Return the finished scan once; later informs get an empty list."""
        with self._state.lock:
            results = self._state.pending
            self._state.pending = None
        return [neighbour.to_inform() for neighbour in results or ()]

    def scan_now(self) -> tuple[NeighbourAp, ...]:
        interfaces = self._ap_interfaces()
        own = {interface.mac for interface in interfaces if interface.mac}
        seen: dict[str, NeighbourAp] = {}
        for interface in _one_per_band(interfaces):
            output = self._run(["iw", "dev", interface.ifname, "scan", "ap-force"])
            if output is None:
                continue
            for neighbour in parse_scan(output, interface.band):
                if neighbour.bssid not in own:
                    seen[neighbour.bssid] = neighbour
        return tuple(seen.values())

    def _scan(self) -> None:
        started = self._clock()
        results: tuple[NeighbourAp, ...] = ()
        try:
            results = self.scan_now()
        except Exception:  # noqa: BLE001 - a failed scan must not kill the agent
            log.exception("Rogue AP scan failed")
        finally:
            with self._state.lock:
                self._state.running = False
                self._state.pending = results
        log.info(
            "Rogue AP scan found %d neighbour AP(s) in %.1f s",
            len(results),
            self._clock() - started,
        )

    def _ap_interfaces(self) -> tuple[ApInterface, ...]:
        output = self._run(["iw", "dev"])
        return parse_ap_interfaces(output or "")

    def _run(self, args: Sequence[str]) -> str | None:
        try:
            result = self._runner.run(list(args))
        except OSError as exc:
            log.warning("%s failed: %s", " ".join(args), exc)
            return None
        if result.returncode != 0:
            log.warning(
                "%s failed (%d): %s",
                " ".join(args),
                result.returncode,
                result.stderr.strip(),
            )
            return None
        return result.stdout


def _one_per_band(interfaces: tuple[ApInterface, ...]) -> tuple[ApInterface, ...]:
    chosen: dict[RadioBand, ApInterface] = {}
    for interface in interfaces:
        chosen.setdefault(interface.band, interface)
    return tuple(chosen.values())


def _daemon_thread(target: Callable[[], None]) -> None:
    threading.Thread(target=target, name="rogue-ap-scan", daemon=True).start()
