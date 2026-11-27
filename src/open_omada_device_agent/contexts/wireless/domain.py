"""Platform-independent wireless configuration model."""
from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping
from ...shared.domain import DomainError, SecretValue

SSID_MAX_BYTES = 32

class InvalidSsid(DomainError):
    pass

@dataclass(frozen=True)
class Ssid:
    value: str
    def __post_init__(self) -> None:
        if len(self.value.encode("utf-8")) > SSID_MAX_BYTES:
            raise InvalidSsid("SSID must fit in 32 UTF-8 bytes")
        if "\x00" in self.value:
            raise InvalidSsid("SSID cannot contain NUL bytes")
    def __str__(self) -> str:
        return self.value

class RadioBand(str, Enum):
    TWO_G = "2g"
    FIVE_G = "5g"
    FIVE_G2 = "5g2"
    SIX_G = "6g"

@dataclass(frozen=True)
class RadioChannel:
    """One channel the radio may use under the current regulatory domain."""
    channel: int
    freq: int
    max_power: int | None = None
    radar: bool = False

@dataclass(frozen=True)
class RadioHardware:
    """PHY capabilities observed for one band (e.g. from ``iw list``)."""
    band: RadioBand
    ht40: bool = False
    vht: bool = False
    vht160: bool = False
    he: bool = False
    he160: bool = False
    # Wi-Fi 7 (802.11be) in AP mode, and 320 MHz channels (6 GHz only).
    eht: bool = False
    eht320: bool = False
    streams: int = 1
    channels: tuple[RadioChannel, ...] = ()
    # Radar detection (CAC) works: the regulatory domain names a DFS region.
    # Without one ("DFS-UNSET", e.g. IN in wireless-regdb) hostapd cannot
    # start CAC and disables the radio on any radar channel.
    dfs: bool = False

@dataclass(frozen=True)
class RadioConfig:
    band: RadioBand
    radio_id: int | None = None
    enabled: bool | None = None
    # Omada chanWidth enum code (e.g. 2=20 MHz, 3=40 MHz, 5=80 MHz), not MHz.
    channel_width: int | None = None
    channel: int | None = None  # 0 means Auto
    # Frequencies (MHz) the controller allows Auto channel selection to use.
    channel_range: tuple[int, ...] = ()
    tx_power: int | None = None
    channel_limit: bool | None = None
    wireless_mode: int | None = None
    raw: Mapping[str, Any] = field(default_factory=dict)

@dataclass(frozen=True)
class ClientLimit:
    """An Omada per-radio limit that can be switched off (``enable``)."""
    enabled: bool
    value: int | None = None

@dataclass(frozen=True)
class RadioAdvancedConfig:
    """Per-radio Advanced settings; ``None`` means "not in this SET"."""
    band: RadioBand
    radio_id: int | None = None
    # Maximum Associated Clients (SET ``loadBalance_*``).
    max_clients: ClientLimit | None = None
    # RSSI Threshold in dBm (SET ``rssi_*``).
    rssi_threshold: ClientLimit | None = None
    # Unscheduled Automatic Power Save Delivery (SET ``qosConfig_*``).
    uapsd: bool | None = None
    # OFDMA (SET ``wirelessAdv_*``); OpenWrt has no switch for it.
    ofdma: bool | None = None

@dataclass(frozen=True)
class WirelessSecurity:
    security_mode: int | None = None
    auth_type: int | None = None
    wpa_version: int | None = None
    wpa_cipher: int | None = None
    psk_version: int | None = None
    psk_cipher: int | None = None
    psk_configured: bool = False
    psk_key: SecretValue | None = field(default=None, repr=False)
    radius_profile_id: str | None = None
    radius_auth: Mapping[str, Any] | None = None
    radius_accounting: Mapping[str, Any] | None = None
    radius_mac_auth: Mapping[str, Any] | None = None
    pmf_mode: int | None = None
    fast_roaming: bool | None = None
    # 802.11r: mobility domain (4 hex digits) and the 128-bit key the
    # controller sends as keyRrb, used for the FT key holders.
    ft_mobility_domain: str | None = None
    ft_key: SecretValue | None = field(default=None, repr=False)
    # The SSID's raw controller data holds the Wi-Fi key: keep it out of repr.
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False)

@dataclass(frozen=True)
class WirelessDhcpOption82Intent:
    enabled: bool = False
    format: int | None = None
    delimiter: str | None = None
    circuit_id: tuple[int, ...] = ()
    remote_id: tuple[int, ...] = ()
    site_name: str | None = None
    raw: Mapping[str, Any] = field(default_factory=dict)

@dataclass(frozen=True)
class WirelessVlanIntent:
    """Wireless-context view of an SSID's network attachment."""
    vlan_id: int | None = None
    vlan_pool_ids: tuple[str, ...] = ()
    dynamic_vlan_mode: int | None = None
    dhcp_option82: WirelessDhcpOption82Intent | None = None

@dataclass(frozen=True)
class CaptivePortalIntent:
    """Wireless-context view of captive access for one SSID."""
    enabled: bool = False
    https_redirect: bool | None = None
    hotspot_v2: Mapping[str, Any] | None = None
    # The SSID's raw controller data holds the Wi-Fi key: keep it out of repr.
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False)

@dataclass(frozen=True)
class WirelessNetwork:
    band: RadioBand
    radio_id: int | None
    ssid_id: int | None
    index: int | None
    operation: int | None
    name: str
    broadcast: bool | None
    client_isolation: bool | None
    vlan: WirelessVlanIntent
    security: WirelessSecurity
    portal: CaptivePortalIntent
    # The SSID's raw controller data holds the Wi-Fi key: keep it out of repr.
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False)

def validate_ssid_name(name: str) -> str:
    return Ssid(name).value
