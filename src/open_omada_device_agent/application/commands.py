"""Typed aggregate commands crossing configuration bounded contexts."""
from dataclasses import dataclass, field
from typing import Any, Mapping

from ..contexts.clients.domain import ClientAuthConfig, ClientControlOperation, ClientRateConfig
from ..contexts.device.domain import (
    LedConfig, LldpConfig, NetworkCheckRequest, SnmpConfig, TimeZoneSetting, WifiControlLedConfig,
)
from ..contexts.networking.domain import LanIpSetting, ManagementVlan
from ..contexts.portal.domain import PortalConfiguration, PortalFreePolicy
from ..contexts.wireless.domain import RadioAdvancedConfig, RadioBand, RadioConfig, WirelessNetwork

@dataclass(frozen=True)
class ApplyDeviceConfigurationCommand:
    sequence_id: int | None
    config_version: int | None
    config_version_inc: int | None
    radios: tuple[RadioConfig, ...] = ()
    wlans: tuple[WirelessNetwork, ...] = ()
    # Bands whose ssid_* key was sent: that list is the band's complete SSID set.
    wlan_bands: tuple[RadioBand, ...] = ()
    management_vlan: ManagementVlan | None = None
    portal_free_policy: PortalFreePolicy | None = None
    portal_configs: tuple[PortalConfiguration, ...] = ()
    led: LedConfig | None = None
    wifi_control_led: WifiControlLedConfig | None = None
    client_configs: tuple[ClientAuthConfig, ...] = ()
    client_operations: tuple[ClientControlOperation, ...] = ()
    client_rate_config: ClientRateConfig | None = None
    rogue_ap_scan: bool = False
    network_checks: tuple[NetworkCheckRequest, ...] = ()
    snmp: SnmpConfig | None = None
    lldp: LldpConfig | None = None
    time_zone: TimeZoneSetting | None = None
    lan_ip: LanIpSetting | None = None
    radio_advanced: tuple[RadioAdvancedConfig, ...] = ()
    passive_keys: tuple[str, ...] = ()
    ack_only_keys: tuple[str, ...] = ()
    unhandled_keys: tuple[str, ...] = ()
    raw_body: Mapping[str, Any] = field(default_factory=dict)
