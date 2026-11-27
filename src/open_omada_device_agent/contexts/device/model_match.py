"""Pick the Omada AP model closest to this device's radios.

The table (``omada_models_data``) has, per model, the Wi-Fi generation and per
radio the band, spatial streams, widest channel and transmit power range. A
model must have the same bands as the device. Among those, the best model
claims nothing the hardware cannot do (newer generation, more streams, wider
channels, more power) and then falls short of it as little as possible.
Variants (``-Outdoor``, ``-Wall``, ``-Bridge``, ...) only win when nothing
plainer fits as well.
"""
from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from .omada_models_data import MODELS

WIDTH_STEPS = (20, 40, 80, 160, 320)


@dataclass(frozen=True)
class RadioFacts:
    band: str  # "2g", "5g" or "6g"
    nss: int
    max_width: int
    max_tx: int | None = None  # dBm; None if unknown


@dataclass(frozen=True)
class ModelMatch:
    model: str
    hw_version: str
    overclaim: int
    shortfall: int

    @property
    def exact(self) -> bool:
        return self.overclaim == 0 and self.shortfall == 0


def _step(width: int) -> int:
    return WIDTH_STEPS.index(width) if width in WIDTH_STEPS else 0


def _pair(device: Sequence[RadioFacts], model: Sequence[dict]) -> list[tuple[RadioFacts, dict]] | None:
    """Pair radios band by band (in order within a band); None if the bands differ."""
    if sorted(r.band for r in device) != sorted(r["band"] for r in model):
        return None
    pairs = []
    for band in sorted({r.band for r in device}):
        ours = [r for r in device if r.band == band]
        theirs = [r for r in model if r["band"] == band]
        pairs.extend(zip(ours, theirs))
    return pairs


def _score(gen: int, device: Sequence[RadioFacts], row: dict) -> tuple[int, int, int] | None:
    pairs = _pair(device, row["radios"])
    if pairs is None:
        return None
    over = max(0, row["gen"] - gen)
    under = max(0, gen - row["gen"])
    power = 0
    for radio, theirs in pairs:
        over += max(0, theirs["nss"] - radio.nss)
        under += max(0, radio.nss - theirs["nss"])
        width = _step(theirs["max_width"]) - _step(radio.max_width)
        over += max(0, width)
        under += max(0, -width)
        if radio.max_tx is not None and theirs["max_tx"]:
            power += abs(theirs["max_tx"] - radio.max_tx)
    return over, under, power


def best_model(
    gen: int,
    radios: Sequence[RadioFacts],
    models: Iterable[dict] = MODELS,
) -> ModelMatch | None:
    """The closest model for a device of Wi-Fi ``gen`` with ``radios``; None if no
    model has the same bands."""
    ranked = []
    for row in models:
        score = _score(gen, radios, row)
        if score is None:
            continue
        variant = "-" in row["model"]
        ranked.append(((score[0], score[1], variant, score[2], row["model"]), row))
    if not ranked:
        return None
    key, row = min(ranked, key=lambda item: item[0])
    return ModelMatch(row["model"], row["hw_version"], overclaim=key[0], shortfall=key[1])
