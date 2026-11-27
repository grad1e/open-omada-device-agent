"""Cross-context configuration orchestration and outbound ports."""
import logging
from dataclasses import dataclass
from typing import Protocol

from .commands import ApplyDeviceConfigurationCommand
from .contracts import PlatformCapabilities

log = logging.getLogger("open_omada.config")

class ReconciliationResult(Protocol):
    applied: bool
    changed: bool
    error: str

class ConfigurationPort(Protocol):
    def reconcile(self, update: ApplyDeviceConfigurationCommand, capabilities: PlatformCapabilities) -> ReconciliationResult: ...

class CapabilityDetector(Protocol):
    def __call__(self) -> PlatformCapabilities: ...

@dataclass(frozen=True)
class ApplyConfigurationResult:
    applied: bool
    changed: bool = False
    error: str = ""

class ApplyDeviceConfiguration:
    def __init__(self, *, capability_detector: CapabilityDetector, platform_ports: tuple[ConfigurationPort, ...], command_ports: tuple[ConfigurationPort, ...], allow_ack_only_config: bool = False) -> None:
        self._detect = capability_detector
        self._platform_ports = platform_ports
        self._command_ports = command_ports
        self._allow_ack_only_config = allow_ack_only_config

    def execute(self, update: ApplyDeviceConfigurationCommand) -> ApplyConfigurationResult:
        # Unsupported keys still fail the SET, but only after the supported
        # domains in the same SET are applied: one unknown key must not leave
        # SSID or radio changes behind.
        unsupported = f"unsupported keys: {','.join(update.unhandled_keys)}" if update.unhandled_keys else ""
        if update.passive_keys:
            log.info(
                "Preserving passive AP config domains without local OpenWrt changes: %s",
                ",".join(update.passive_keys),
            )
        has_platform = bool(update.radios or update.radio_advanced or update.wlans or update.wlan_bands or update.management_vlan is not None or update.portal_free_policy is not None)
        has_commands = bool(update.led is not None or update.wifi_control_led is not None or update.client_configs or update.client_operations or update.client_rate_config is not None or update.rogue_ap_scan or update.network_checks or update.snmp is not None or update.lldp is not None or update.time_zone is not None or update.lan_ip is not None)
        changed = False
        capabilities = self._detect()
        ports = (self._platform_ports if has_platform else ()) + (self._command_ports if has_commands else ())
        soft_errors: list[str] = []
        for port in ports:
            result = port.reconcile(update, capabilities)
            if not result.applied:
                return ApplyConfigurationResult(False, changed, "; ".join(filter(None, (result.error, unsupported))))
            changed = changed or result.changed
            if result.error:
                soft_errors.append(result.error)
        errors = list(soft_errors)
        if update.ack_only_keys:
            keys = ",".join(update.ack_only_keys)
            if self._allow_ack_only_config:
                log.warning("Acknowledging controller-side AP config without local OpenWrt changes: %s", keys)
            else:
                errors.append(f"ack-only control-plane keys require OMADA_LAB_ACK_CONTROL_PLANE_CONFIG=true: {keys}")
        if unsupported:
            errors.append(unsupported)
        if errors:
            if changed and len(errors) > len(soft_errors):
                errors.append("supported domains were applied first")
            return ApplyConfigurationResult(False, changed, "; ".join(errors))
        return ApplyConfigurationResult(True, changed)
