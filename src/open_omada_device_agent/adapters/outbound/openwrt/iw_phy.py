"""Parse ``iw list`` output into per-band radio hardware capabilities."""
from __future__ import annotations

import re
from dataclasses import replace

from ....contexts.wireless.domain import RadioBand, RadioChannel, RadioHardware

_BAND_HEADER = re.compile(r"^\tBand \d+:\s*$")
_CHANNEL = re.compile(r"\*\s+(\d+(?:\.\d+)?)\s+MHz\s+\[(\d+)\](.*)$")
_POWER = re.compile(r"\((\d+(?:\.\d+)?)\s*dBm\)")
_STREAMS = re.compile(r"(\d+)\s+streams:\s+MCS")
_HT_MCS = re.compile(r"HT TX/RX MCS rate indexes supported:\s*0-(\d+)")
_REG_COUNTRY = re.compile(r"^country\s+\S+:\s*(DFS-\w+)", re.MULTILINE)

# DFS regions under which the kernel can run radar detection.
DFS_REGIONS = frozenset({"DFS-FCC", "DFS-ETSI", "DFS-JP"})


def parse_dfs_region(output: str) -> str:
    """DFS region of the first (global) country in ``iw reg get`` output."""
    match = _REG_COUNTRY.search(output)
    return match.group(1) if match else ""


def parse_iw_list(output: str) -> tuple[RadioHardware, ...]:
    """Return one ``RadioHardware`` per band found, in ``iw list`` order.

    A second radio on 5 GHz (tri-band) is reported as ``RadioBand.FIVE_G2``.
    """
    radios: list[RadioHardware] = []
    seen: set[RadioBand] = set()
    for section in _band_sections(output):
        radio = _parse_band(section)
        if radio is None:
            continue
        if radio.band in seen:
            if radio.band is not RadioBand.FIVE_G or RadioBand.FIVE_G2 in seen:
                continue
            radio = replace(radio, band=RadioBand.FIVE_G2)
        seen.add(radio.band)
        radios.append(radio)
    return tuple(radios)


def _band_sections(output: str) -> list[list[str]]:
    sections: list[list[str]] = []
    current: list[str] | None = None
    for line in output.splitlines():
        if _BAND_HEADER.match(line):
            current = []
            sections.append(current)
            continue
        if current is None:
            continue
        # A line indented by exactly one tab (or a new wiphy) ends the band.
        if line.startswith("Wiphy") or (line.startswith("\t") and not line.startswith("\t\t")):
            current = None
            continue
        current.append(line)
    return sections


def _parse_band(lines: list[str]) -> RadioHardware | None:
    channels = tuple(_channels(lines))
    band = _band_for(channels)
    if band is None:
        return None
    text = "\n".join(lines)
    he_ap = _ap_block(lines, "HE")
    he = bool(he_ap)
    he_text = "\n".join(he_ap)
    eht_text = "\n".join(_ap_block(lines, "EHT"))
    vht = "VHT Capabilities" in text
    return RadioHardware(
        band=band,
        ht40="HT20/HT40" in text,
        vht=vht,
        vht160=vht and bool(re.search(r"Supported Channel Width:.*160 MHz", text)),
        he=he,
        he160=he and "HE160/" in he_text,
        eht=bool(eht_text),
        eht320=band is RadioBand.SIX_G
        and ("320MHz in 6GHz Supported" in eht_text or "EHT bw=320 MHz" in eht_text),
        streams=_streams(text, he_text, vht=vht),
        channels=channels,
    )


def _channels(lines: list[str]):
    for line in lines:
        match = _CHANNEL.search(line)
        if match is None:
            continue
        rest = match.group(3)
        # "no IR": the regulatory domain forbids initiating transmission, so an
        # AP cannot use the channel (typical with the unset "00" world domain).
        if "disabled" in rest or "no IR" in rest:
            continue
        power = _POWER.search(rest)
        yield RadioChannel(
            channel=int(match.group(2)),
            freq=int(float(match.group(1))),
            max_power=int(float(power.group(1))) if power else None,
            radar="radar detection" in rest,
        )


def _band_for(channels: tuple[RadioChannel, ...]) -> RadioBand | None:
    if not channels:
        return None
    freq = channels[0].freq
    if freq < 2500:
        return RadioBand.TWO_G
    if freq < 5925:
        return RadioBand.FIVE_G
    if freq < 7200:
        return RadioBand.SIX_G
    return None


def _ap_block(lines: list[str], kind: str) -> list[str]:
    """Lines of the ``HE Iftypes`` or ``EHT Iftypes`` block for AP mode.

    A block ends at the next ``HE``/``EHT Iftypes`` line or the band's next
    section (``Bitrates``, ``Frequencies``).
    """
    block: list[str] = []
    inside = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith(("HE Iftypes:", "EHT Iftypes:")) and not stripped.startswith(f"{kind} Iftypes:"):
            inside = False
            continue
        if stripped.startswith(("Bitrates", "Frequencies")):
            inside = False
            continue
        if stripped.startswith(f"{kind} Iftypes:"):
            modes = re.split(r"[,\s]+", stripped.split(":", 1)[1].strip())
            inside = "AP" in modes
            if inside:
                block.append(stripped)
            continue
        if inside:
            block.append(stripped)
    return block


def _streams(text: str, he_text: str, *, vht: bool) -> int:
    for source in (he_text, text if vht else ""):
        counts = [int(n) for n in _STREAMS.findall(source)]
        if counts:
            return max(counts)
    ht = _HT_MCS.search(text)
    if ht:
        return max(1, (int(ht.group(1)) + 1) // 8)
    return 1

