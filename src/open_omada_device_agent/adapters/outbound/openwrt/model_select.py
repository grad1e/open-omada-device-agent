"""``OMADA_DEVICE_MODEL=auto``: report the Omada model closest to the radios."""
from __future__ import annotations

import logging
from dataclasses import replace

from ....application.contracts import PlatformCapabilities
from ....application.settings import AgentSettings
from ....contexts.device.model_match import ModelMatch, RadioFacts, best_model
from ....contexts.wireless.domain import RadioBand, RadioHardware

log = logging.getLogger("open_omada.model")

AUTO = "auto"
_BANDS = {
    RadioBand.TWO_G: "2g",
    RadioBand.FIVE_G: "5g",
    RadioBand.FIVE_G2: "5g",
    RadioBand.SIX_G: "6g",
}


def radio_facts(hardware: RadioHardware) -> RadioFacts:
    two_g = hardware.band is RadioBand.TWO_G
    # 80 and 160 MHz channels exist only above 2.4 GHz; some 2.4 GHz chips
    # still report VHT ("256-QAM on 2.4 GHz").
    if not two_g and (hardware.he160 or hardware.vht160):
        width = 160
    elif not two_g and (hardware.vht or hardware.he):
        width = 80
    elif hardware.ht40:
        width = 40
    else:
        width = 20
    powers = [c.max_power for c in hardware.channels if c.max_power is not None]
    return RadioFacts(
        band=_BANDS[hardware.band],
        nss=hardware.streams,
        max_width=width,
        max_tx=max(powers) if powers else None,
    )


def generation(radios: tuple[RadioHardware, ...]) -> int:
    if any(r.he for r in radios):
        return 6
    if any(r.vht for r in radios):
        return 5
    return 4


def match_for(capabilities: PlatformCapabilities) -> ModelMatch | None:
    hardware = capabilities.radio_hardware
    if not hardware:
        return None
    return best_model(generation(hardware), [radio_facts(r) for r in hardware])


def resolve_model(settings: AgentSettings, capabilities: PlatformCapabilities) -> AgentSettings:
    """With ``OMADA_DEVICE_MODEL=auto``, the closest model and the model and
    hardware versions a real one sends;
    otherwise ``settings`` unchanged (the closest model is only logged)."""
    match = match_for(capabilities)
    if settings.model.strip().lower() != AUTO:
        if match is not None and match.model != settings.model:
            log.info(
                "Closest Omada model for these radios: %s (OMADA_DEVICE_MODEL=auto would use it)",
                match.model,
            )
        return settings
    if match is None:
        log.warning(
            "OMADA_DEVICE_MODEL=auto: no radios found or no model with these bands; using EAP110"
        )
        return replace(settings, model="EAP110", model_version="4.0", hardware_version="4.0")
    log.info(
        "OMADA_DEVICE_MODEL=auto: using %s (model version %s, hardware version %s; %s)",
        match.model,
        match.model_version,
        match.hardware_version,
        "exact match" if match.exact else
        f"{match.shortfall} step(s) below the hardware, {match.overclaim} above",
    )
    return replace(
        settings,
        model=match.model,
        model_version=match.model_version,
        hardware_version=match.hardware_version,
    )
