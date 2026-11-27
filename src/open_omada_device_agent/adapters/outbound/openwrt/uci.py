"""OpenWrt platform adapter for AP WLAN configuration.

This module is deliberately below the Omada domain layer.  ECSP parsers produce
domain objects; this adapter turns supported domain objects into UCI changes.
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

from .... import config
from ....application.commands import ApplyDeviceConfigurationCommand
from ....contexts.wireless.domain import (
    MAC_FILTER_ANY_LIST, MAC_FILTER_DELETE, MAC_FILTER_OPERATIONS, MAC_FILTER_TYPE_GROUP,
    MacFilterChange, MacFilterPolicy, MacFilterState,
    CCK_RATES, RadioAdvancedConfig, RadioBand, RadioConfig, RadioHardware, RateControl,
    WirelessNetwork, WirelessSecurity,
)
from ....contexts.wireless.schedule import (
    SCHEDULE_DELETE, SCHEDULE_OPERATIONS, SCHEDULE_SELECT_DAYS, SCHEDULE_TYPES, MINUTES_PER_DAY,
    ScheduleChange, ScheduleState,
)

from ....application.contracts import PlatformCapabilities
from ....shared.domain import SecretValue


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
        portal_network: str = "",
        state_file: Path | None = None,
        clock: Callable[[], datetime] = datetime.now,
    ) -> None:
        self._runner = runner or SubprocessRunner()
        self._management_vlan_interface = management_vlan_interface
        self._management_vlan_device = management_vlan_device
        self._portal_network = portal_network
        # MAC filter groups, WLAN schedules and their SSID links as last
        # applied. Changes arrive one rule at a time, so the state is saved to
        # survive an agent restart.
        self._state_file = state_file
        self._mac_filter, self._schedule = _load_wireless_state(state_file)
        # Local time in the site time zone the agent wrote to OpenWrt. A
        # running agent follows a zone change: OpenWrt's libc re-reads
        # /etc/TZ (Observed: Jio, offset changed without a restart).
        self._clock = clock
        self._ticked = False

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
        for wlan in update.wlans:
            log.info(
                "SSID %s %s security: pskVer=%s pskCipher=%s pmfMode=%s 802.11r=%s mobilityDomain=%s"
                " loadBalance=%s",
                wlan.band.value,
                wlan.index if wlan.index is not None else wlan.ssid_id,
                wlan.security.psk_version,
                wlan.security.psk_cipher,
                wlan.security.pmf_mode,
                wlan.security.fast_roaming,
                wlan.security.ft_mobility_domain,
                wlan.load_balance,
            )
        mac_filter = self._mac_filter
        if update.mac_filter is not None:
            errors = (*errors, *_mac_filter_errors(update.mac_filter))
            mac_filter = mac_filter.apply(update.mac_filter, full_sync=update.config_version is not None)
        schedule = self._schedule
        if update.schedule is not None:
            errors = (*errors, *_schedule_errors(update.schedule))
            schedule = schedule.apply(update.schedule, full_sync=update.config_version is not None)
            if update.schedule.mode not in (None, 0):
                log.warning("schedulerGlobal mode %s is Unknown; applying it like mode 0", update.schedule.mode)
        if errors:
            return self._refuse(update, capabilities, errors)

        for radio in update.radios:
            log.info(
                "Radio %s settings: enabled=%s wirelessMode=%s chanWidth=%s channel=%s txPower=%s",
                radio.band.value,
                radio.enabled,
                radio.wireless_mode,
                radio.channel_width,
                radio.channel,
                radio.tx_power,
            )
        for advanced in update.radio_advanced:
            if advanced.ofdma is False:
                log.info(
                    "OFDMA off requested for %s; OpenWrt has no switch for it, "
                    "the driver keeps OFDMA on for Wi-Fi 6",
                    advanced.band.value,
                )
        if any(wlan.load_balance for wlan in update.wlans):
            log.info(
                "Load Balance on: 802.11k%s advertised only; no steering daemon "
                "(usteer) moves clients",
                "/v" if capabilities.supports_80211v else "",
            )
        wireless_state = self._wireless_state(update)
        radios = radio_sections_by_band(wireless_state)
        missing = _missing_radio_errors(update, capabilities, radios)
        if missing:
            return ReconciliationResult(applied=False, changed=False, error="; ".join(missing))
        batch = build_uci_batch(
            update,
            capabilities,
            management_vlan_interface=self._management_vlan_interface,
            management_vlan_device=self._management_vlan_device,
            current_htmodes=self._current_htmodes(update, radios),
            wireless_state=wireless_state,
            portal_network=self._portal_network,
            mac_filter=mac_filter,
            mac_filter_changed=update.mac_filter is not None,
            schedule=schedule,
            schedule_changed=update.schedule is not None,
            now=self._clock(),
        )
        if not batch:
            self._commit_state(mac_filter, schedule)
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
        # UCI now holds the new filter; a failed reload does not undo that.
        self._commit_state(mac_filter, schedule)

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

    def _refuse(
        self,
        update: ApplyDeviceConfigurationCommand,
        capabilities: PlatformCapabilities,
        errors: Sequence[str],
    ) -> ReconciliationResult:
        """Refuse the SET, but first remove open SSIDs it refuses for security.

        Before securityMode was checked, an SSID the agent cannot secure
        (WPA-Enterprise, PPSK, OWE, WPA-Personal without a key) was written as
        an open network. A refused SET writes nothing, so such a section would
        stay open until the SSID is changed in the controller. The section is
        deleted, not just disabled: the WLAN schedule tick would otherwise
        turn a disabled section back on. The next accepted SET rebuilds it.
        """
        error = "; ".join(errors)
        refused = {
            _wlan_section(wlan)
            for wlan in _active_wlans(update, capabilities)
            if _security_mode_errors(wlan)
        }
        if not refused:
            return ReconciliationResult(applied=False, changed=False, error=error)
        result = self._runner.run(["uci", "-q", "show", "wireless"])
        if result.returncode != 0:
            log.warning("uci show wireless failed; cannot check refused SSIDs for open sections")
            return ReconciliationResult(applied=False, changed=False, error=error)
        wireless_state = parse_uci_show(result.stdout)
        stale = sorted(section for section in refused if _open_agent_ssid(wireless_state.get(section, {})))
        if not stale:
            return ReconciliationResult(applied=False, changed=False, error=error)
        lines = [f"delete wireless.{section}" for section in stale]
        lines.append("commit wireless")
        batch = self._runner.run(["uci", "-q", "batch"], input_text="\n".join(lines) + "\n")
        if batch.returncode != 0:
            detail = (batch.stderr or batch.stdout or "uci batch failed").strip()
            log.warning("Could not remove open SSID sections %s: %s", ", ".join(stale), detail)
            return ReconciliationResult(applied=False, changed=False, error=error, command_count=len(lines))
        log.warning("Removed open SSID sections %s: their SSIDs are refused for security", ", ".join(stale))
        reload_result = self._runner.run(["wifi", "reload"])
        if reload_result.returncode != 0:
            detail = (reload_result.stderr or reload_result.stdout or "wifi reload failed").strip()
            log.warning("wifi reload after removing open SSIDs failed: %s", detail)
        return ReconciliationResult(
            applied=False,
            changed=True,
            error=f"{error}; removed the open SSID left by an earlier apply",
            command_count=len(lines),
        )

    def tick(self) -> ReconciliationResult | None:
        """Turn scheduled SSIDs off or back on; None when nothing changed.

        Called about once a minute. Reads UCI only while a schedule is linked
        (and once after start, to undo a stale schedule).
        """
        if self._ticked and not (self._schedule.enabled and self._schedule.assoc):
            return None
        result = self._runner.run(["uci", "-q", "show", "wireless"])
        if result.returncode != 0:
            return None
        self._ticked = True
        wireless_state = parse_uci_show(result.stdout)
        now = self._clock()
        lines: list[str] = []
        for section, ssid_id, band in _managed_ssid_sections(wireless_state):
            off = self._schedule.ssid_off(ssid_id, band, now)
            section_lines = _schedule_lines(section, wireless_state[section], off)
            if section_lines:
                log.info("WLAN schedule: SSID section %s %s", section, "off" if off else "back on")
            lines.extend(section_lines)
        if not lines:
            return None
        lines.append("commit wireless")
        batch = self._runner.run(["uci", "-q", "batch"], input_text="\n".join(lines) + "\n")
        if batch.returncode != 0:
            error = (batch.stderr or batch.stdout or "uci batch failed").strip()
            log.warning("WLAN schedule not applied: %s", error)
            return ReconciliationResult(applied=False, changed=False, error=error, command_count=len(lines))
        reload_result = self._runner.run(["wifi", "reload"])
        if reload_result.returncode != 0:
            error = (reload_result.stderr or reload_result.stdout or "wifi reload failed").strip()
            log.warning("WLAN schedule: %s", error)
            return ReconciliationResult(applied=False, changed=True, error=error, command_count=len(lines))
        return ReconciliationResult(applied=True, changed=True, command_count=len(lines))

    def _commit_state(self, mac_filter: MacFilterState, schedule: ScheduleState) -> None:
        if mac_filter == self._mac_filter and schedule == self._schedule:
            return
        self._mac_filter = mac_filter
        self._schedule = schedule
        if self._state_file is None:
            return
        data = {"macFilter": mac_filter.to_dict(), "schedule": schedule.to_dict()}
        try:
            self._state_file.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = self._state_file.with_name(self._state_file.name + ".tmp")
            tmp_path.write_text(json.dumps(data, sort_keys=True) + "\n")
            os.replace(tmp_path, self._state_file)
        except OSError as exc:
            log.warning("Could not save wireless state to %s: %s", self._state_file, exc)

    def _wireless_state(self, update: ApplyDeviceConfigurationCommand) -> WirelessState:
        """Read ``wireless`` sections when radios or SSIDs are involved."""
        if not (update.radios or update.radio_advanced or update.wlans or update.wlan_bands or update.mac_filter or update.schedule):
            return {}
        result = self._runner.run(["uci", "-q", "show", "wireless"])
        if result.returncode != 0:
            if update.mac_filter is not None:
                log.warning("uci show wireless failed; SSIDs this SET does not rebuild keep their old MAC filter")
            if update.wlan_bands:
                log.warning("uci show wireless failed; 802.11 Rate Control the agent set before is not removed")
            return {}
        return parse_uci_show(result.stdout)

    def _current_htmodes(
        self,
        update: ApplyDeviceConfigurationCommand,
        radios: Mapping[RadioBand, str],
    ) -> dict[str, str]:
        """Read the current UCI htmode of radios whose channel width will change."""
        htmodes: dict[str, str] = {}
        for radio in update.radios:
            if radio.channel_width is None and radio.wireless_mode is None:
                continue
            section = _radio_section(radio.band, radio.radio_id, radios)
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
        errors.extend(_security_mode_errors(wlan))
        if _wlan_psk_key(wlan) is not None:
            errors.extend(_psk_errors(wlan, capabilities))
        if wlan.load_balance and not capabilities.supports_80211k:
            errors.append("Load Balance requested but the 802.11k capability is disabled")
        errors.extend(_rate_control_errors(wlan))
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
    portal_network: str = "",
    mac_filter: MacFilterState | None = None,
    mac_filter_changed: bool = False,
    schedule: ScheduleState | None = None,
    schedule_changed: bool = False,
    now: datetime | None = None,
) -> tuple[str, ...]:
    lines: list[str] = []
    mac_filter = mac_filter or MacFilterState()
    schedule = schedule or ScheduleState()
    now = now or datetime.now()
    current_htmodes = current_htmodes or {}
    wireless_state = wireless_state or {}
    radios = radio_sections_by_band(wireless_state)
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
        section = _radio_section(radio.band, radio.radio_id, radios)
        lines.extend(
            _radio_lines(
                radio,
                section,
                current_htmodes.get(section),
                advertised_channels=usable_channels(capabilities, radio.band),
                hardware=capabilities.hardware_for(radio.band),
            )
        )
    for band in update.wlan_bands:
        lines.extend(_rate_control_lines(band, active_wlans, wireless_state, radios))
    radio_uapsd = _radio_uapsd(wireless_state)
    rebuilt = {_wlan_section(wlan) for wlan in active_wlans}
    for advanced in update.radio_advanced:
        section = _radio_section(advanced.band, advanced.radio_id, radios)
        if advanced.uapsd is not None:
            radio_uapsd[section] = advanced.uapsd
        lines.extend(_radio_advanced_lines(advanced, section, wireless_state, skip=rebuilt))
    for section in _default_sections(active_wlans, radios):
        lines.append(_set("wireless", section, "disabled", "1"))
    for wlan in omitted_wlans:
        lines.append(f"delete wireless.{_wlan_section(wlan)}")
    omitted = {_wlan_section(wlan) for wlan in omitted_wlans}
    removed = _removed_ssid_sections(update.wlan_bands, rebuilt | omitted, wireless_state)
    for section in removed:
        lines.append(f"delete wireless.{section}")
    if mac_filter_changed:
        # SSIDs this SET does not rebuild still follow the new MAC filter.
        for section, ssid_id, band in _managed_ssid_sections(wireless_state):
            if section in rebuilt or section in omitted or section in removed:
                continue
            policy = mac_filter.policy(ssid_id, band)
            lines.extend(_mac_filter_lines(section, policy, reset=True))
    if schedule_changed:
        # SSIDs this SET does not rebuild still follow the new schedule.
        for section, ssid_id, band in _managed_ssid_sections(wireless_state):
            if section in rebuilt or section in omitted or section in removed:
                continue
            off = schedule.ssid_off(ssid_id, band, now)
            lines.extend(_schedule_lines(section, wireless_state[section], off))
    for wlan in active_wlans:
        lines.extend(_wlan_lines(wlan, capabilities, portal_network, radios))
        band_index = MAC_FILTER_BANDS.get(wlan.band)
        if band_index is not None:
            policy = mac_filter.policy(wlan.ssid_id, band_index)
            lines.extend(_mac_filter_lines(_wlan_section(wlan), policy, reset=False))
            if schedule.ssid_off(wlan.ssid_id, band_index, now):
                lines.extend(_schedule_lines(_wlan_section(wlan), {}, True))
        uapsd = radio_uapsd.get(_radio_section(wlan.band, wlan.radio_id, radios))
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


def _removed_ssid_sections(
    bands: tuple[RadioBand, ...],
    keep: set[str],
    wireless_state: WirelessState,
) -> tuple[str, ...]:
    """Agent-created SSIDs on ``bands`` that the controller no longer lists."""
    prefixes = tuple(f"openomada_{band.value}_" for band in bands)
    return tuple(
        name
        for name, options in wireless_state.items()
        if prefixes
        and name.startswith(prefixes)
        and name not in keep
        and options.get(".type") == "wifi-iface"
        and options.get("openomada_managed") == "1"
    )


# macFilterAssoc and schedulerAssoc ``band``: 0 = 2.4 GHz, 1 = 5 GHz (Observed). Other bands are
# Unknown and refused.
MAC_FILTER_BANDS = {RadioBand.TWO_G: 0, RadioBand.FIVE_G: 1}
_MAC = re.compile(r"^[0-9a-fA-F]{2}([-:]?[0-9a-fA-F]{2}){5}$")


def _mac_filter_errors(change: MacFilterChange) -> list[str]:
    errors = []
    for rule in change.list_rules:
        if rule.operation not in MAC_FILTER_OPERATIONS:
            errors.append(f"unsupported macFilterList operation {rule.operation}")
        if rule.mac is not None and not _MAC.match(rule.mac):
            errors.append("macFilterList has an invalid MAC address")
    for rule in change.assoc_rules:
        if rule.operation not in MAC_FILTER_OPERATIONS:
            errors.append(f"unsupported macFilterAssoc operation {rule.operation}")
        if rule.operation != MAC_FILTER_DELETE:
            if rule.band not in MAC_FILTER_BANDS.values():
                errors.append(f"unsupported macFilterAssoc band {rule.band}")
            if rule.type != MAC_FILTER_TYPE_GROUP:
                errors.append("MAC filter OUI profiles are not supported (hostapd filters exact MACs only)")
            if rule.action not in (0, 1):
                errors.append(f"unsupported macFilterAssoc action {rule.action}")
        elif rule.list_id != MAC_FILTER_ANY_LIST and rule.list_id < 0:
            errors.append(f"unsupported macFilterAssoc listId {rule.list_id}")
    return errors


def _mac_filter_lines(section: str, policy: MacFilterPolicy | None, *, reset: bool) -> tuple[str, ...]:
    lines = []
    if reset:
        lines.append(f"delete wireless.{section}.macfilter")
        lines.append(f"delete wireless.{section}.maclist")
    if policy is None:
        return tuple(lines)
    lines.append(_set("wireless", section, "macfilter", "allow" if policy.allow else "deny"))
    for mac in policy.macs:
        lines.append(f"add_list wireless.{section}.maclist='{_normal_mac(mac)}'")
    return tuple(lines)


def _load_wireless_state(state_file: Path | None) -> tuple[MacFilterState, ScheduleState]:
    if state_file is None or not state_file.exists():
        return MacFilterState(), ScheduleState()
    try:
        data = json.loads(state_file.read_text())
    except (OSError, ValueError) as exc:
        log.warning("Ignoring unreadable wireless state %s: %s", state_file, exc)
        return MacFilterState(), ScheduleState()
    mac_filter, schedule = MacFilterState(), ScheduleState()
    try:
        mac_filter = MacFilterState.from_dict(data["macFilter"])
    except (KeyError, ValueError, TypeError, AttributeError) as exc:
        log.warning("Ignoring unreadable MAC filter state in %s: %s", state_file, exc)
    try:
        if "schedule" in data:
            schedule = ScheduleState.from_dict(data["schedule"])
    except (KeyError, ValueError, TypeError, AttributeError) as exc:
        log.warning("Ignoring unreadable WLAN schedule state in %s: %s", state_file, exc)
    return mac_filter, schedule


# wifi-iface option marking an SSID the WLAN schedule turned off, so the agent
# only ever turns back on what it turned off itself.
SCHEDULE_OFF_OPTION = "openomada_schedule_off"


def _schedule_lines(section: str, options: Mapping[str, str], off: bool) -> tuple[str, ...]:
    marked = options.get(SCHEDULE_OFF_OPTION) == "1"
    if off and not marked:
        return (
            _set("wireless", section, "disabled", "1"),
            _set("wireless", section, SCHEDULE_OFF_OPTION, "1"),
        )
    if not off and marked:
        return (f"delete wireless.{section}.disabled", f"delete wireless.{section}.{SCHEDULE_OFF_OPTION}")
    return ()


def _schedule_errors(change: ScheduleChange) -> list[str]:
    errors = []
    for rule in change.list_rules:
        if rule.operation not in SCHEDULE_OPERATIONS:
            errors.append(f"unsupported schedulerList operation {rule.operation}")
        period = rule.period
        if period is None or rule.operation == SCHEDULE_DELETE:
            continue
        if period.type not in SCHEDULE_TYPES and period.type != SCHEDULE_SELECT_DAYS:
            errors.append(f"unsupported schedulerList type {period.type!r}")
        elif period.type == SCHEDULE_SELECT_DAYS and not 1 <= period.day <= 7:
            errors.append(f"unsupported schedulerList day {period.day}")
        if not (0 <= period.start < MINUTES_PER_DAY and 0 <= period.end <= MINUTES_PER_DAY):
            errors.append("schedulerList has a time outside 00:00-24:00")
    for rule in change.assoc_rules:
        if rule.profile_id is None:
            continue
        if rule.band not in MAC_FILTER_BANDS.values():
            errors.append(f"unsupported schedulerAssoc band {rule.band}")
        if rule.action not in (0, 1):
            errors.append(f"unsupported schedulerAssoc action {rule.action}")
    return errors


def _normal_mac(mac: str) -> str:
    digits = re.sub(r"[^0-9a-fA-F]", "", mac).lower()
    return ":".join(digits[i:i + 2] for i in range(0, 12, 2))


def _managed_ssid_sections(wireless_state: WirelessState) -> tuple[tuple[str, int | None, int], ...]:
    """(section, controller SSID id, macFilterAssoc band) for agent SSIDs."""
    sections = []
    for name, options in wireless_state.items():
        if options.get(".type") != "wifi-iface" or options.get("openomada_managed") != "1":
            continue
        for band, index in MAC_FILTER_BANDS.items():
            if name.startswith(f"openomada_{band.value}_"):
                raw_id = options.get("openomada_ssid_id", "")
                ssid_id = int(raw_id) if raw_id.lstrip("-").isdigit() else None
                sections.append((name, ssid_id, index))
    return tuple(sections)


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


def _default_sections(
    wlans: tuple[WirelessNetwork, ...],
    radios: Mapping[RadioBand, str],
) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            f"default_{_radio_section(wlan.band, wlan.radio_id, radios)}"
            for wlan in wlans
        )
    )


def _radio_lines(
    radio: RadioConfig,
    section: str,
    current_htmode: str | None = None,
    *,
    advertised_channels: tuple[int, ...] = (),
    hardware: RadioHardware | None = None,
) -> tuple[str, ...]:
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
    if radio.channel_width is not None or radio.wireless_mode is not None:
        htmode = _radio_htmode(radio, current_htmode, hardware)
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


def _wlan_lines(
    wlan: WirelessNetwork,
    capabilities: PlatformCapabilities,
    portal_network: str = "",
    radios: Mapping[RadioBand, str] | None = None,
) -> tuple[str, ...]:
    section = _wlan_section(wlan)
    device = _radio_section(wlan.band, wlan.radio_id, radios or {})
    network = "lan"
    if wlan.vlan.vlan_id is not None and capabilities.supports_ssid_vlan:
        network = f"openomada_vlan{wlan.vlan.vlan_id}"
    elif wlan.portal.enabled and portal_network:
        # A routed guest network keeps openNDS off the AP's uplink bridge.
        network = portal_network

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

    psk_key = _wlan_psk_key(wlan)
    if psk_key is not None:
        lines.append(_set("wireless", section, "encryption", _psk_encryption(wlan.security)))
        lines.append(_set("wireless", section, "key", psk_key.reveal()))
        lines.extend(_pmf_lines(section, wlan.security))
        lines.extend(_fast_transition_lines(section, wlan.security))
    else:
        lines.append(_set("wireless", section, "encryption", "none"))
    if wlan.load_balance:
        # 802.11k neighbour and beacon reports, plus 802.11v BSS transition
        # management when hostapd has it (wpad-basic refuses bss_transition and
        # the radio stays down). They let a steering daemon (usteer) move
        # clients; hostapd alone only advertises them.
        lines.append(_set("wireless", section, "ieee80211k", "1"))
        if capabilities.supports_80211v:
            lines.append(_set("wireless", section, "bss_transition", "1"))
    return tuple(lines)


# Omada WPA-Personal codes (Observed, lab, Controller 6.3.0.111, SSID editor
# "WPA Mode"): pskVer 1 = WPA-PSK, 2 = WPA2-PSK, 3 = WPA/WPA2-PSK,
# 4 = WPA2-PSK/WPA3-SAE; pskCipher 1 = Auto (TKIP+AES), 3 = AES. The editor
# offers no TKIP-only choice, so code 2 is refused rather than guessed.
# SSID item ``securityMode`` (Observed: 3 = WPA-Personal in lab SETs; 0 =
# None, 2 = WPA-Enterprise, 4 = PPSK without RADIUS and 5 = PPSK with RADIUS
# are Inferred from the controller's Security list). OWE is None plus
# ``oweEnable``. Only None and WPA-Personal are applied: anything else would
# otherwise come up as an open network, since OpenWrt gets no key for it.
SECURITY_NONE = 0
SECURITY_WPA_ENTERPRISE = 2
SECURITY_WPA_PERSONAL = 3
SECURITY_PPSK = (4, 5)


def _wlan_psk_key(wlan: WirelessNetwork) -> SecretValue | None:
    """The Wi-Fi key to write, or None for an open SSID.

    The security mode decides: a None SSID stays open even if the controller
    still sends an old key. Without a mode (older controllers) the key does.
    """
    security = wlan.security
    if security.security_mode not in (None, SECURITY_WPA_PERSONAL):
        return None
    key = security.psk_key
    return key if key is not None and key.reveal() else None


def _security_mode_errors(wlan: WirelessNetwork) -> list[str]:
    security = wlan.security
    mode = security.security_mode
    if mode == SECURITY_NONE:
        if _truthy(security.raw.get("oweEnable")):
            return [f"SSID {wlan.name!r}: OWE (Enhanced Open) is not supported"]
        return []
    if mode == SECURITY_WPA_PERSONAL:
        if _wlan_psk_key(wlan) is None:
            return [f"SSID {wlan.name!r}: WPA-Personal without a Wi-Fi key; not creating it as an open network"]
        return []
    if mode is None:
        return []
    if mode == SECURITY_WPA_ENTERPRISE:
        return [f"SSID {wlan.name!r}: WPA-Enterprise is not supported"]
    if mode in SECURITY_PPSK:
        return [f"SSID {wlan.name!r}: PPSK is not supported"]
    return [f"SSID {wlan.name!r}: unsupported security mode code {mode}"]


def _open_agent_ssid(options: Mapping[str, str]) -> bool:
    """An agent-created SSID section with no encryption, on or off.

    A disabled one counts too: the WLAN schedule may turn it back on.
    """
    return (
        options.get(".type") == "wifi-iface"
        and options.get("openomada_managed") == "1"
        and options.get("encryption", "none") == "none"
    )


def _truthy(value: object) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "enable", "enabled"}
    return bool(value)


PSK_ENCRYPTION = {1: "psk", 2: "psk2", 3: "psk-mixed", 4: "sae-mixed"}
PSK_CIPHER_SUFFIX = {1: "+tkip+ccmp", 3: "+ccmp"}
PSK_VERSION_WPA3 = 4
# pmfMode 1 = Mandatory, 2 = Capable, 3 = Disable (Observed, same editor) ->
# hostapd ieee80211w 2 / 1 / 0.
PMF_IEEE80211W = {1: "2", 2: "1", 3: "0"}
_HEX = re.compile(r"^[0-9a-fA-F]+$")


def _psk_errors(wlan: WirelessNetwork, capabilities: PlatformCapabilities) -> list[str]:
    security = wlan.security
    errors = []
    version = security.psk_version
    if version is not None and version not in PSK_ENCRYPTION:
        errors.append(f"unsupported WPA-Personal version code {version}")
    elif version == PSK_VERSION_WPA3 and not capabilities.supports_wpa3_psk:
        errors.append("WPA3-PSK WLAN requested but WPA3-PSK capability is disabled")
    elif version != PSK_VERSION_WPA3 and not capabilities.supports_wpa2_psk:
        errors.append("PSK WLAN requested but WPA2-PSK capability is disabled")
    key_error = _psk_key_error(security.psk_key.reveal() if security.psk_key is not None else "")
    if key_error:
        errors.append(f"SSID {wlan.name!r}: {key_error}")
    if security.psk_cipher is not None and security.psk_cipher not in PSK_CIPHER_SUFFIX:
        errors.append(f"unsupported WPA cipher code {security.psk_cipher}")
    if security.pmf_mode is not None and security.pmf_mode not in PMF_IEEE80211W:
        errors.append(f"unsupported PMF mode code {security.pmf_mode}")
    # WPA (v1) gets no 802.11r lines, so a leftover enable11r is not checked.
    if security.fast_roaming and version != 1:
        if not capabilities.supports_80211r:
            errors.append("802.11r requested but the 802.11r capability is disabled (hostapd without 11r)")
        domain = security.ft_mobility_domain or ""
        if len(domain) != 4 or not _HEX.match(domain):
            errors.append("802.11r needs a 4-digit hex mobility domain")
        key = security.ft_key.reveal() if security.ft_key is not None else ""
        if key and (len(key) not in (32, 64) or not _HEX.match(key)):
            errors.append("802.11r key from the controller is not 32 or 64 hex digits")
    return errors


def _psk_key_error(key: str) -> str | None:
    """Why OpenWrt would reject this Wi-Fi key, or None. Never echoes the key.

    OpenWrt takes 8-63 bytes as a passphrase and 64 characters as a raw PSK;
    anything else fails the interface with INVALID_WPA_PSK (Observed: OpenWrt
    25.12 ``/usr/share/ucode/wifi/ap.uc`` and ``/lib/netifd/hostapd.sh``).
    hostapd reads a raw PSK as hex, and its config is one setting per line, so
    control characters are refused too.
    """
    if not key:
        return None
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in key):
        return "Wi-Fi key contains a control character"
    size = len(key.encode("utf-8"))
    if size == 64:
        return None if _HEX.match(key) else "a 64-character Wi-Fi key must be hex digits"
    if not 8 <= size <= 63:
        return f"Wi-Fi key must be 8 to 63 characters (or 64 hex digits), got {size} bytes"
    return None


def _psk_encryption(security: WirelessSecurity) -> str:
    version = security.psk_version
    if version is None:
        return "psk2"  # older controllers send no version
    encryption = PSK_ENCRYPTION[version]
    if version == PSK_VERSION_WPA3:
        return encryption  # SAE is AES only; OpenWrt picks CCMP
    return encryption + PSK_CIPHER_SUFFIX.get(security.psk_cipher or 0, "")


def _pmf_lines(section: str, security: WirelessSecurity) -> tuple[str, ...]:
    # WPA (v1) has no RSN, so no PMF. WPA3 needs PMF: OpenWrt already enables
    # it for sae-mixed, and the controller hides "Disable" there.
    if security.psk_version == 1 or security.pmf_mode not in PMF_IEEE80211W:
        return ()
    value = PMF_IEEE80211W[security.pmf_mode]
    if security.psk_version == PSK_VERSION_WPA3 and value == "0":
        return ()
    return (_set("wireless", section, "ieee80211w", value),)


def _fast_transition_lines(section: str, security: WirelessSecurity) -> tuple[str, ...]:
    # FT needs RSN (WPA2 or later); the controller hides 802.11r for WPA-PSK.
    if not security.fast_roaming or security.psk_version == 1:
        return ()
    lines = [
        _set("wireless", section, "ieee80211r", "1"),
        _set("wireless", section, "mobility_domain", (security.ft_mobility_domain or "").lower()),
        _set("wireless", section, "ft_over_ds", "0"),
    ]
    if security.psk_version != PSK_VERSION_WPA3:
        # FT-PSK only: each AP derives the keys from the PSK, no key exchange.
        lines.append(_set("wireless", section, "ft_psk_generate_local", "1"))
        return tuple(lines)
    # FT-SAE cannot derive keys locally. OpenWrt (wifi/ap.uc, hostapd.sh) reads
    # r0kh/r1kh only when ft_psk_generate_local is 0; without them it derives a key from
    # the mobility domain and the PSK, which only other OpenWrt APs share.
    lines.append(_set("wireless", section, "ft_psk_generate_local", "0"))
    if security.ft_key is not None:
        # Every AP in the site gets the same key from the controller.
        key = security.ft_key.reveal().lower()
        lines.append(f"add_list wireless.{section}.r0kh='ff:ff:ff:ff:ff:ff,*,{uci_quote(key)}'")
        lines.append(
            f"add_list wireless.{section}.r1kh='00:00:00:00:00:00,00:00:00:00:00:00,{uci_quote(key)}'"
        )
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


# wifi-device option marking rate options the agent wrote, so it only removes
# its own.
RATE_CONTROL_OPTION = "openomada_rates"
_RATE_OPTIONS = ("supported_rates", "basic_rate", "beacon_rate")


def _band_rates(rate_control: RateControl, band: RadioBand) -> tuple[int, ...]:
    """Allowed rates on ``band``: 802.11b (CCK) rates exist on 2.4 GHz only."""
    if band == RadioBand.TWO_G:
        return rate_control.rates
    return tuple(rate for rate in rate_control.rates if rate not in CCK_RATES)


def _rate_control_errors(wlan: WirelessNetwork) -> list[str]:
    control = wlan.rate_control
    if control is None:
        return []
    errors = []
    if control.unknown_bits:
        errors.append(f"unsupported 802.11 Rate Control bits {control.unknown_bits:#x}")
    if control.enabled and not _band_rates(control, wlan.band):
        errors.append(f"802.11 Rate Control on {wlan.band.value} allows no rate")
    if (
        control.enabled
        and control.management_rate is not None
        and wlan.band != RadioBand.TWO_G
        and control.management_rate in CCK_RATES
    ):
        # hostapd would find no basic rate the radio can use and not start it.
        errors.append(f"802.11 management rate {control.management_rate} kb/s does not exist on {wlan.band.value}")
    return errors


def _rate_control_lines(
    band: RadioBand,
    active_wlans: Sequence[WirelessNetwork],
    wireless_state: WirelessState,
    radios: Mapping[RadioBand, str],
) -> tuple[str, ...]:
    """Radio rate options for ``band``: hostapd sets rates per radio, not per SSID."""
    wlans = [wlan for wlan in active_wlans if wlan.band == band]
    sections = {_radio_section(wlan.band, wlan.radio_id, radios) for wlan in wlans}
    if not sections and (band in radios or not radios):
        # No SSIDs left on the band: still clear the rates the agent set.
        sections = {_radio_section(band, None, radios)}
    lines: list[str] = []
    for section in sorted(sections):
        on_radio = [wlan for wlan in wlans if _radio_section(wlan.band, wlan.radio_id, radios) == section]
        wanted = {
            wlan.rate_control if wlan.rate_control is not None and wlan.rate_control.enabled else None
            for wlan in on_radio
        }
        control = next(iter(wanted)) if len(wanted) == 1 else None
        if len(wanted) > 1:
            log.warning(
                "SSIDs on %s ask for different 802.11 Rate Control; OpenWrt sets rates per radio, "
                "so the radio keeps its default rates",
                section,
            )
        owned = wireless_state.get(section, {}).get(RATE_CONTROL_OPTION) == "1"
        if control is None:
            if owned:
                lines.extend(f"delete wireless.{section}.{option}" for option in (*_RATE_OPTIONS, RATE_CONTROL_OPTION))
            continue
        rates = _band_rates(control, band)
        supported = sorted(set(rates) | ({control.management_rate} if control.management_rate else set()))
        if control.management_rate:
            basic = sorted({control.management_rate} | (set(rates) if control.require_minimum else set()))
        else:
            basic = list(rates) if control.require_minimum else [rates[0]]
        lines.extend(f"delete wireless.{section}.{option}" for option in _RATE_OPTIONS)
        lines.extend(f"add_list wireless.{section}.supported_rates='{rate}'" for rate in supported)
        lines.extend(f"add_list wireless.{section}.basic_rate='{rate}'" for rate in basic)
        if control.beacon_1mbps and band == RadioBand.TWO_G:
            # 1 Mbps exists on 2.4 GHz only. hostapd beacon_rate is in 100 kb/s units (Validated: Jio, mt76).
            lines.append(_set("wireless", section, "beacon_rate", "10"))
        lines.append(_set("wireless", section, RATE_CONTROL_OPTION, "1"))
    return tuple(lines)


# UCI wifi-device ``band`` -> the Omada bands its radios fill, in order. A
# second 5 GHz radio is the controller's 5 GHz-2.
_UCI_BAND_SLOTS = {
    "2g": (RadioBand.TWO_G,),
    "5g": (RadioBand.FIVE_G, RadioBand.FIVE_G2),
    "6g": (RadioBand.SIX_G,),
}


def assign_radio_bands(uci_bands: Mapping[str, str]) -> dict[RadioBand, str]:
    """Omada band -> radio name, from each radio's UCI band (``2g``, ``5g``, ``6g``).

    Board radio order varies (radio0 is 5 GHz on many boards), so the band
    option decides. Two radios on one band are sorted by name: the first is
    5 GHz, the second 5 GHz-2. Radios without a known band are left out.
    """
    by_band: dict[str, list[str]] = {}
    for name, uci_band in uci_bands.items():
        if uci_band in _UCI_BAND_SLOTS:
            by_band.setdefault(uci_band, []).append(name)
    radios: dict[RadioBand, str] = {}
    for uci_band, names in by_band.items():
        for slot, name in zip(_UCI_BAND_SLOTS[uci_band], sorted(names, key=_natural_key)):
            radios[slot] = name
    return radios


def radio_sections_by_band(wireless_state: WirelessState) -> dict[RadioBand, str]:
    """Omada band -> ``wifi-device`` section, from ``uci show wireless``."""
    return assign_radio_bands(
        {
            name: options.get("band", "")
            for name, options in wireless_state.items()
            if options.get(".type") == "wifi-device"
        }
    )


def _natural_key(name: str) -> tuple[object, ...]:
    return tuple(int(part) if part.isdigit() else part for part in re.split(r"(\d+)", name))


def _missing_radio_errors(
    update: ApplyDeviceConfigurationCommand,
    capabilities: PlatformCapabilities,
    radios: Mapping[RadioBand, str],
) -> list[str]:
    """Bands the SET configures that no radio has, when radios carry a band."""
    if not radios:
        return []
    bands = [radio.band for radio in update.radios]
    bands += [advanced.band for advanced in update.radio_advanced]
    bands += [wlan.band for wlan in _active_wlans(update, capabilities)]
    return [f"no wifi-device with the {band.value} band in UCI" for band in dict.fromkeys(bands) if band not in radios]


def _radio_section(band: RadioBand, radio_id: int | None, radios: Mapping[RadioBand, str]) -> str:
    """The ``wifi-device`` for ``band``; radio number only when UCI has no bands."""
    if band in radios:
        return radios[band]
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

_HTMODE_FAMILY = re.compile(r"^(NOHT|EHT|HE|VHT|HT)", re.IGNORECASE)
_HTMODE_MHZ = re.compile(r"(\d+)$")

# Omada ``wirelessBasic_*.wirelessMode`` codes (Controller 6.3.0.111 UI enum;
# 3, 4, 7 and 13 confirmed in the lab). "Auto" is sent as no key at all.
# Each mode maps to the newest OpenWrt htmode family it allows.
OMADA_WIRELESS_MODE_FAMILY: dict[int, str] = {
    3: "NOHT",  # 802.11b/g mixed
    4: "HT",  # 802.11b/g/n mixed
    13: "HE",  # 802.11b/g/n/ax mixed
    17: "EHT",  # 802.11b/g/n/ax/be mixed
    7: "HT",  # 802.11a/n mixed
    10: "VHT",  # 802.11a/n/ac mixed
    16: "HE",  # 802.11a/n/ac/ax mixed
    18: "EHT",  # 802.11a/n/ac/ax/be mixed
}
_FAMILY_RANK = {"NOHT": 0, "HT": 1, "VHT": 2, "HE": 3, "EHT": 4}


def _hardware_family(band: RadioBand, hardware: RadioHardware | None) -> str | None:
    if hardware is None:
        return None
    if hardware.he:
        return "HE"
    if hardware.vht and band is not RadioBand.TWO_G:
        return "VHT"
    return "HT"


def _radio_htmode(
    radio: RadioConfig,
    current_htmode: str | None = None,
    hardware: RadioHardware | None = None,
) -> str | None:
    """OpenWrt htmode for the radio's Omada wireless mode and channel width.

    Auto mode (no ``wirelessMode``) uses the newest family the hardware
    supports; an explicit mode is capped at it. Without hardware details the
    radio's current family is kept, so an 802.11ax radio is not downgraded.
    """
    if radio.channel_width is not None:
        mhz = OMADA_CHANNEL_WIDTH_MHZ.get(radio.channel_width)
    else:
        match = _HTMODE_MHZ.search(current_htmode or "")
        mhz = int(match.group(1)) if match else 20
    if mhz is None:
        return None
    best = _hardware_family(radio.band, hardware)
    requested = OMADA_WIRELESS_MODE_FAMILY.get(radio.wireless_mode or 0)
    current = _HTMODE_FAMILY.match(current_htmode or "")
    if requested is not None:
        family = requested
        if best is not None and _FAMILY_RANK[family] > _FAMILY_RANK[best]:
            family = best
    elif best is not None:
        family = best
    elif current and current.group(1).upper() != "NOHT":
        family = current.group(1).upper()
    else:
        family = "HT" if radio.band is RadioBand.TWO_G else "VHT"
    if family == "NOHT":
        return "NOHT"
    if family == "VHT" and radio.band is RadioBand.TWO_G:
        family = "HT"
    if family == "HT" and mhz > 40:
        if requested is not None or best is not None:
            mhz = 40  # 802.11n stops at 40 MHz
        else:
            family = "VHT"
    return f"{family}{mhz}"


def _set(config: str, section: str, option: str, value: str) -> str:
    return f"set {config}.{section}.{option}='{uci_quote(value)}'"


def uci_quote(value: str) -> str:
    """Escape ``value`` for use inside single quotes in a ``uci batch`` line.

    libuci reads everything between single quotes literally (uci/file.c,
    parse_single_quote), so only the quote itself needs escaping: close the
    quote, add an escaped quote, reopen. A backslash stays as it is.
    """
    return value.replace("'", "'\\''")


def _slug(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "_", value).strip("_").lower()
