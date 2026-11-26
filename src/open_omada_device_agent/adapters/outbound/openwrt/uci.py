"""OpenWrt platform adapter for AP WLAN configuration.

This module is deliberately below the Omada domain layer.  ECSP parsers produce
domain objects; this adapter turns supported domain objects into UCI changes.
"""
from __future__ import annotations

import logging
import re
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from .... import config
from ....application.commands import ApplyDeviceConfigurationCommand
from ....contexts.wireless.domain import (
    RadioAdvancedConfig, RadioBand, RadioConfig, WirelessNetwork,
)

from ....application.contracts import PlatformCapabilities


log = logging.getLogger("open_omada.openwrt.uci")

# wifi-device option that keeps the controller's U-APSD choice for a radio.
# SSID sections are rebuilt on every SSID change, so ``uapsd`` is re-applied
# to them from here.
UAPSD_RADIO_OPTION = "openomada_uapsd"
# Omada RSSI Threshold range (dBm) accepted for hostapd rssi_reject_assoc_rssi.
RSSI_THRESHOLD_RANGE = range(-100, 0)

WirelessState = Mapping[str, Mapping[str, str]]


class CommandRunner(Protocol):
    def run(self, args: Sequence[str], *, input_text: str | None = None) -> "CommandResult":
        ...


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


@dataclass(frozen=True)
class ReconciliationResult:
    applied: bool
    changed: bool
    error: str = ""
    command_count: int = 0


class SubprocessRunner:
    def run(self, args: Sequence[str], *, input_text: str | None = None) -> CommandResult:
        completed = subprocess.run(
            list(args),
            input=input_text,
            text=True,
            capture_output=True,
            check=False,
        )
        return CommandResult(
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )


class OpenWrtUciAdapter:
    def __init__(
        self,
        runner: CommandRunner | None = None,
        *,
        management_vlan_interface: str | None = None,
        management_vlan_device: str | None = None,
    ) -> None:
        self._runner = runner or SubprocessRunner()
        self._management_vlan_interface = management_vlan_interface
        self._management_vlan_device = management_vlan_device

    def reconcile(
        self,
        update: ApplyDeviceConfigurationCommand,
        capabilities: PlatformCapabilities,
    ) -> ReconciliationResult:
        capacity_warning = _capacity_warning(update, capabilities)
        if capacity_warning:
            log.warning(capacity_warning)
        errors = validate_update(
            update,
            capabilities,
            management_vlan_interface=self._management_vlan_interface,
            management_vlan_device=self._management_vlan_device,
        )
        if errors:
            return ReconciliationResult(
                applied=False,
                changed=False,
                error="; ".join(errors),
            )

        for advanced in update.radio_advanced:
            if advanced.ofdma is False:
                log.info(
                    "OFDMA off requested for %s; OpenWrt has no switch for it, "
                    "the driver keeps OFDMA on for Wi-Fi 6",
                    advanced.band.value,
                )
        batch = build_uci_batch(
            update,
            capabilities,
            management_vlan_interface=self._management_vlan_interface,
            management_vlan_device=self._management_vlan_device,
            current_htmodes=self._current_htmodes(update),
            wireless_state=self._wireless_state(update),
        )
        if not batch:
            return ReconciliationResult(
                applied=True,
                changed=False,
            )

        result = self._runner.run(["uci", "-q", "batch"], input_text="\n".join(batch) + "\n")
        if result.returncode != 0:
            return ReconciliationResult(
                applied=False,
                changed=False,
                error=(result.stderr or result.stdout or "uci batch failed").strip(),
                command_count=len(batch),
            )

        reload_result = self._runner.run(["wifi", "reload"])
        if reload_result.returncode != 0:
            return ReconciliationResult(
                applied=False,
                changed=True,
                error=(reload_result.stderr or reload_result.stdout or "wifi reload failed").strip(),
                command_count=len(batch),
            )

        return ReconciliationResult(
            applied=True,
            changed=True,
            command_count=len(batch),
        )

    def _wireless_state(self, update: ApplyDeviceConfigurationCommand) -> WirelessState:
        """Read ``wireless`` sections when Advanced radio settings are involved."""
        if not (update.radio_advanced or update.wlans):
            return {}
        result = self._runner.run(["uci", "-q", "show", "wireless"])
        if result.returncode != 0:
            return {}
        return parse_uci_show(result.stdout)

    def _current_htmodes(self, update: ApplyDeviceConfigurationCommand) -> dict[str, str]:
        """Read the current UCI htmode of radios whose channel width will change."""
        htmodes: dict[str, str] = {}
        for radio in update.radios:
            if radio.channel_width is None:
                continue
            section = _radio_section(radio.band, radio.radio_id)
            result = self._runner.run(["uci", "-q", "get", f"wireless.{section}.htmode"])
            if result.returncode == 0 and result.stdout.strip():
                htmodes[section] = result.stdout.strip()
        return htmodes


def validate_update(
    update: ApplyDeviceConfigurationCommand,
    capabilities: PlatformCapabilities,
    *,
    management_vlan_interface: str | None = None,
    management_vlan_device: str | None = None,
) -> tuple[str, ...]:
    errors: list[str] = []
    if (update.radios or update.wlans) and not capabilities.supports_wlan_config:
        errors.append("platform does not support WLAN configuration")
    for radio in update.radios:
        if radio.band not in capabilities.radio_bands:
            errors.append(f"radio band {radio.band.value} is not supported")
    for wlan in _active_wlans(update, capabilities):
        if wlan.band not in capabilities.radio_bands:
            errors.append(f"SSID band {wlan.band.value} is not supported")
        if wlan.vlan.vlan_id is not None and not capabilities.supports_ssid_vlan:
            errors.append("SSID VLAN requested but platform capability is disabled")
        if (
            wlan.vlan.dynamic_vlan_mode is not None
            and wlan.vlan.dynamic_vlan_mode != 0
            and not capabilities.supports_dynamic_vlan
        ):
            errors.append("dynamic VLAN requested but platform capability is disabled")
        if (
            wlan.vlan.dhcp_option82 is not None
            and wlan.vlan.dhcp_option82.enabled
            and not capabilities.supports_option82
        ):
            errors.append("DHCP Option 82 requested but platform capability is disabled")
        if wlan.portal.enabled and not capabilities.supports_portal:
            errors.append("portal WLAN requested but platform capability is disabled")
        if (
            wlan.security.psk_key is not None
            and wlan.security.psk_version == 3
            and not capabilities.supports_wpa3_psk
        ):
            errors.append("WPA3-PSK WLAN requested but WPA3-PSK capability is disabled")
        if (
            wlan.security.psk_key is not None
            and wlan.security.psk_version != 3
            and not capabilities.supports_wpa2_psk
        ):
            errors.append("PSK WLAN requested but WPA2-PSK capability is disabled")
        if (
            wlan.security.radius_auth is not None
            or wlan.security.radius_accounting is not None
            or wlan.security.radius_mac_auth is not None
        ) and not capabilities.supports_radius:
            errors.append("RADIUS WLAN requested but RADIUS capability is disabled")
    if update.radio_advanced and not capabilities.supports_wlan_config:
        errors.append("platform does not support WLAN configuration")
    for advanced in update.radio_advanced:
        if advanced.band not in capabilities.radio_bands:
            errors.append(f"radio band {advanced.band.value} is not supported")
        limit = advanced.max_clients
        if limit is not None and limit.enabled and (limit.value is None or limit.value < 1):
            errors.append(f"Maximum Associated Clients for {advanced.band.value} needs a value of 1 or more")
        rssi = advanced.rssi_threshold
        if rssi is not None and rssi.enabled and rssi.value not in RSSI_THRESHOLD_RANGE:
            errors.append(f"RSSI Threshold for {advanced.band.value} must be -100 to -1 dBm")
    if (
        update.management_vlan is not None
        and update.management_vlan.enabled
        and not capabilities.supports_management_vlan
    ):
        errors.append("management VLAN requested but platform capability is disabled")
    if (
        update.management_vlan is not None
        and update.management_vlan.enabled
        and capabilities.supports_management_vlan
    ):
        if update.management_vlan.vlan_id is None:
            errors.append("enabled management VLAN is missing managementVlanId")
        interface = config.MANAGEMENT_VLAN_INTERFACE if management_vlan_interface is None else management_vlan_interface
        device = config.MANAGEMENT_VLAN_DEVICE if management_vlan_device is None else management_vlan_device
        if not interface or not device:
            errors.append(
                "OMADA_MANAGEMENT_VLAN_INTERFACE and OMADA_MANAGEMENT_VLAN_DEVICE are required"
            )
    return tuple(dict.fromkeys(errors))


def build_uci_batch(
    update: ApplyDeviceConfigurationCommand,
    capabilities: PlatformCapabilities,
    *,
    management_vlan_interface: str | None = None,
    management_vlan_device: str | None = None,
    current_htmodes: Mapping[str, str] | None = None,
    wireless_state: WirelessState | None = None,
) -> tuple[str, ...]:
    lines: list[str] = []
    current_htmodes = current_htmodes or {}
    wireless_state = wireless_state or {}
    network_lines: list[str] = []
    active_wlans = _active_wlans(update, capabilities)
    omitted_wlans = tuple(update.wlans[len(active_wlans):])
    vlan_ids = sorted(
        {
            wlan.vlan.vlan_id
            for wlan in active_wlans
            if wlan.vlan.vlan_id is not None and capabilities.supports_ssid_vlan
        }
    )
    for vlan_id in vlan_ids:
        network_lines.extend(_ssid_vlan_lines(vlan_id))
    if update.management_vlan is not None:
        network_lines.extend(
            _management_vlan_lines(
                update,
                interface=(config.MANAGEMENT_VLAN_INTERFACE if management_vlan_interface is None else management_vlan_interface),
                device=(config.MANAGEMENT_VLAN_DEVICE if management_vlan_device is None else management_vlan_device),
            )
        )
    if network_lines:
        lines.extend(network_lines)
        lines.append("commit network")
    for radio in update.radios:
        section = _radio_section(radio.band, radio.radio_id)
        lines.extend(
            _radio_lines(
                radio,
                current_htmodes.get(section),
                advertised_channels=usable_channels(capabilities, radio.band),
            )
        )
    radio_uapsd = _radio_uapsd(wireless_state)
    rebuilt = {_wlan_section(wlan) for wlan in active_wlans}
    for advanced in update.radio_advanced:
        section = _radio_section(advanced.band, advanced.radio_id)
        if advanced.uapsd is not None:
            radio_uapsd[section] = advanced.uapsd
        lines.extend(_radio_advanced_lines(advanced, section, wireless_state, skip=rebuilt))
    for section in _default_sections(active_wlans):
        lines.append(_set("wireless", section, "disabled", "1"))
    for wlan in omitted_wlans:
        lines.append(f"delete wireless.{_wlan_section(wlan)}")
    for wlan in active_wlans:
        lines.extend(_wlan_lines(wlan, capabilities))
        uapsd = radio_uapsd.get(_radio_section(wlan.band, wlan.radio_id))
        if uapsd is not None:
            lines.append(_set("wireless", _wlan_section(wlan), "uapsd", "1" if uapsd else "0"))
    if lines:
        lines.append("commit wireless")
    return tuple(lines)


def _radio_advanced_lines(
    advanced: RadioAdvancedConfig,
    section: str,
    wireless_state: WirelessState,
    *,
    skip: set[str],
) -> tuple[str, ...]:
    """UCI lines for Maximum Associated Clients, RSSI Threshold and U-APSD."""
    lines: list[str] = []
    current = wireless_state.get(section, {})
    limits = (
        # Radio-wide client limit (hostapd iface_max_num_sta).
        ("maxassoc", advanced.max_clients),
        # Refuse association below this signal (hostapd rssi_reject_assoc_rssi).
        ("rssi_reject_assoc_rssi", advanced.rssi_threshold),
    )
    for option, limit in limits:
        if limit is None:
            continue
        if limit.enabled and limit.value is not None:
            lines.append(_set("wireless", section, option, str(limit.value)))
        elif option in current:
            lines.append(f"delete wireless.{section}.{option}")
    if advanced.uapsd is not None:
        value = "1" if advanced.uapsd else "0"
        lines.append(_set("wireless", section, UAPSD_RADIO_OPTION, value))
        for iface in _managed_ifaces(wireless_state, section):
            if iface not in skip:
                lines.append(_set("wireless", iface, "uapsd", value))
    return tuple(lines)


def _radio_uapsd(wireless_state: WirelessState) -> dict[str, bool]:
    return {
        name: options[UAPSD_RADIO_OPTION] == "1"
        for name, options in wireless_state.items()
        if options.get(".type") == "wifi-device" and UAPSD_RADIO_OPTION in options
    }


def _managed_ifaces(wireless_state: WirelessState, device: str) -> tuple[str, ...]:
    return tuple(
        name
        for name, options in wireless_state.items()
        if options.get(".type") == "wifi-iface"
        and options.get("device") == device
        and options.get("openomada_managed") == "1"
    )


_UCI_SHOW_LINE = re.compile(r"^wireless\.([^.=]+)(?:\.([^=]+))?=(.*)$")


def parse_uci_show(text: str) -> dict[str, dict[str, str]]:
    """Parse ``uci show wireless`` into {section: {".type": ..., option: value}}."""
    sections: dict[str, dict[str, str]] = {}
    for line in text.splitlines():
        match = _UCI_SHOW_LINE.match(line.strip())
        if match is None:
            continue
        name, option, value = match.groups()
        value = value.strip("'")
        section = sections.setdefault(name, {})
        if option is None:
            section[".type"] = value
        else:
            section[option] = value
    return sections


def _capacity_warning(
    update: ApplyDeviceConfigurationCommand,
    capabilities: PlatformCapabilities,
) -> str:
    if len(update.wlans) <= capabilities.max_ssids:
        return ""
    return (
        f"controller requested {len(update.wlans)} SSIDs but platform max is "
        f"{capabilities.max_ssids}; applied first {capabilities.max_ssids}"
    )


def _active_wlans(
    update: ApplyDeviceConfigurationCommand,
    capabilities: PlatformCapabilities,
) -> tuple[WirelessNetwork, ...]:
    return tuple(update.wlans[: max(0, capabilities.max_ssids)])


def _default_sections(wlans: tuple[WirelessNetwork, ...]) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            f"default_{_radio_section(wlan.band, wlan.radio_id)}"
            for wlan in wlans
        )
    )


def _radio_lines(
    radio: RadioConfig,
    current_htmode: str | None = None,
    *,
    advertised_channels: tuple[int, ...] = (),
) -> tuple[str, ...]:
    section = _radio_section(radio.band, radio.radio_id)
    lines = []
    if radio.enabled is not None:
        lines.append(_set("wireless", section, "disabled", "0" if radio.enabled else "1"))
    if radio.channel == 0:
        # Omada Auto: let OpenWrt ACS choose, but only from the controller's
        # channel range. Without the list ACS may pick a DFS channel the
        # regulatory domain cannot run, and the radio goes down.
        lines.append(_set("wireless", section, "channel", "auto"))
        # channelRange only arrives when the radio is edited in the UI; reconnect
        # and Force Provision SETs omit it. Fall back to the channels the agent
        # advertised (no DFS), which is all the controller believes exists.
        channels = _acs_channels(radio)
        if advertised_channels:
            # The controller may still hold a range from another country or
            # DFS state (e.g. DFS channels saved while radar detection worked).
            channels = tuple(ch for ch in channels if ch in advertised_channels)
        channels = channels or advertised_channels
        if channels:
            lines.append(_set("wireless", section, "channels", " ".join(map(str, channels))))
    elif radio.channel is not None:
        lines.append(_set("wireless", section, "channel", str(radio.channel)))
    if radio.channel_width is not None:
        htmode = _channel_width_mode(radio.channel_width, radio.band, current_htmode)
        if htmode is None:
            log.warning(
                "Unknown Omada chanWidth code %s for %s; leaving htmode unchanged",
                radio.channel_width,
                section,
            )
        else:
            lines.append(_set("wireless", section, "htmode", htmode))
    if radio.tx_power is not None:
        lines.append(_set("wireless", section, "txpower", str(radio.tx_power)))
    return tuple(lines)


def usable_channels(
    capabilities: PlatformCapabilities,
    band: RadioBand,
) -> tuple[int, ...]:
    """Channels the radio may use now: radar channels only if CAC works."""
    hardware = capabilities.hardware_for(band)
    if hardware is None:
        return ()
    return tuple(ch.channel for ch in hardware.channels if hardware.dfs or not ch.radar)


def _acs_channels(radio: RadioConfig) -> tuple[int, ...]:
    channels = (_frequency_to_channel(radio.band, freq) for freq in radio.channel_range)
    return tuple(dict.fromkeys(ch for ch in channels if ch is not None))


def _frequency_to_channel(band: RadioBand, freq: int) -> int | None:
    if band is RadioBand.TWO_G:
        if freq == 2484:
            return 14
        if 2412 <= freq <= 2472:
            return (freq - 2407) // 5
        return None
    if band is RadioBand.SIX_G:
        return (freq - 5950) // 5 if 5955 <= freq <= 7115 else None
    return (freq - 5000) // 5 if 5160 <= freq <= 5885 else None


def _wlan_lines(wlan: WirelessNetwork, capabilities: PlatformCapabilities) -> tuple[str, ...]:
    section = _wlan_section(wlan)
    device = _radio_section(wlan.band, wlan.radio_id)
    network = "lan"
    if wlan.vlan.vlan_id is not None and capabilities.supports_ssid_vlan:
        network = f"openomada_vlan{wlan.vlan.vlan_id}"

    lines = [
        f"delete wireless.{section}",
        f"set wireless.{section}=wifi-iface",
        _set("wireless", section, "openomada_managed", "1"),
        *(
            # Echoed back as ssidStats_*.id in the inform (telemetry.SSID_ID_OPTION).
            (_set("wireless", section, "openomada_ssid_id", str(wlan.ssid_id)),)
            if wlan.ssid_id is not None
            else ()
        ),
        _set("wireless", section, "device", device),
        _set("wireless", section, "mode", "ap"),
        _set("wireless", section, "network", network),
        _set("wireless", section, "ssid", wlan.name),
        _set("wireless", section, "hidden", "0" if wlan.broadcast is not False else "1"),
    ]
    if wlan.client_isolation is not None:
        lines.append(_set("wireless", section, "isolate", "1" if wlan.client_isolation else "0"))

    psk_key = wlan.security.psk_key
    if psk_key is not None:
        encryption = "sae-mixed" if wlan.security.psk_version == 3 else "psk2"
        lines.append(_set("wireless", section, "encryption", encryption))
        lines.append(_set("wireless", section, "key", psk_key.reveal()))
    else:
        lines.append(_set("wireless", section, "encryption", "none"))
    return tuple(lines)


def _ssid_vlan_lines(vlan_id: int) -> tuple[str, ...]:
    section = f"openomada_vlan{vlan_id}"
    return (
        f"delete network.{section}",
        f"set network.{section}=interface",
        _set("network", section, "proto", "none"),
        _set("network", section, "device", f"br-lan.{vlan_id}"),
    )


def _management_vlan_lines(
    update: ApplyDeviceConfigurationCommand,
    *,
    interface: str,
    device: str,
) -> tuple[str, ...]:
    management_vlan = update.management_vlan
    if management_vlan is None:
        return ()
    if not interface or not device:
        return ()
    if not management_vlan.enabled:
        return (
            _set(
                "network",
                interface,
                "device",
                device,
            ),
        )
    if management_vlan.vlan_id is None:
        return ()
    return (
        _set(
            "network",
            interface,
            "device",
            f"{device}.{management_vlan.vlan_id}",
        ),
    )


def _radio_section(band: RadioBand, radio_id: int | None) -> str:
    if radio_id is not None:
        return f"radio{radio_id}"
    return {
        RadioBand.TWO_G: "radio0",
        RadioBand.FIVE_G: "radio1",
        RadioBand.FIVE_G2: "radio2",
        RadioBand.SIX_G: "radio3",
    }[band]


def _wlan_section(wlan: WirelessNetwork) -> str:
    suffix = wlan.index if wlan.index is not None else wlan.ssid_id
    if suffix is None:
        suffix = _slug(wlan.name) or "ssid"
    return _slug(f"openomada_{wlan.band.value}_{suffix}")[:48]


# Omada sends ``wirelessBasic_*.chanWidth`` as an enum code, not MHz.
# Observed: 2 -> 20 MHz (EAP650 radioSetting2g with bandWidth "20MHz"),
# 3 -> 40 MHz (lab, Controller 6.3.0.111), 5 -> 80 MHz (EAP650 radioSetting5g
# with bandWidth "80MHz", and lab), 7 -> 160 MHz (lab, channel 36).
# "Auto" widths (lab, Controller 6.3.0.111): 4 = 20/40 Auto (2.4 GHz),
# 6 = 20/40/80 Auto (5 GHz). They map to the widest width of the set; hostapd
# still narrows on 2.4 GHz when overlapping BSSes forbid 40 MHz.
OMADA_CHANNEL_WIDTH_MHZ: dict[int, int] = {2: 20, 3: 40, 4: 40, 5: 80, 6: 80, 7: 160}

_HTMODE_FAMILY = re.compile(r"^(EHT|HE|VHT|HT)", re.IGNORECASE)


def _channel_width_mode(
    width_code: int,
    band: RadioBand,
    current_htmode: str | None = None,
) -> str | None:
    """Map an Omada chanWidth code to an OpenWrt htmode.

    The radio's current htmode family (EHT/HE/VHT/HT) is kept so that an
    802.11ax radio stays on HE instead of being downgraded to HT/VHT.
    """
    mhz = OMADA_CHANNEL_WIDTH_MHZ.get(width_code)
    if mhz is None:
        return None
    match = _HTMODE_FAMILY.match(current_htmode or "")
    if match:
        family = match.group(1).upper()
    else:
        family = "HT" if band is RadioBand.TWO_G else "VHT"
    if family == "VHT" and band is RadioBand.TWO_G:
        family = "HT"
    if family == "HT" and mhz > 40:
        family = "VHT"
    return f"{family}{mhz}"


def _set(config: str, section: str, option: str, value: str) -> str:
    return f"set {config}.{section}.{option}='{_uci_quote(value)}'"


def _uci_quote(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "'\\''")


def _slug(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "_", value).strip("_").lower()
