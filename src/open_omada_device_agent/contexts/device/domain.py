"""Static device identity and profile contracts."""
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol
from ...shared.domain import MacAddress, SecretValue

@dataclass(frozen=True)
class DeviceIdentity:
    mac: MacAddress
    name: str
    model: str
    model_version: str
    hardware_version: str
    firmware_version: str

@dataclass(frozen=True)
class NetworkCheckRequest:
    """One Tools -> Network Check run (SET keys ``ping``/``traceroute``/``dnslookup``/``arptable``)."""

    kind: str
    cmd_id: str
    dest: str | None = None
    count: int | None = None
    packet_size: int | None = None


@dataclass(frozen=True)
class LldpConfig:
    """SET key ``lldp``: the controller resolves the per-AP/site setting to on or off."""

    enabled: bool = False


MIN_UTC_OFFSET_MINUTES = -12 * 60
MAX_UTC_OFFSET_MINUTES = 14 * 60
# ``timeSetting.timeZone`` is the site's standard UTC offset in minutes plus 720
# (Observed: 1050 for UTC+05:30 on Controller 6.3.0.111).
TIME_ZONE_BASE = 720


@dataclass(frozen=True)
class DaylightSaving:
    """``dst`` from the controller, kept as sent until its format is known."""

    enabled: bool = False
    start: int = 0
    end: int = 0
    offset: int = 0


@dataclass(frozen=True)
class TimeZoneSetting:
    """Site time zone (``timeSetting`` + ``dst``) sent at every connect."""

    utc_offset_minutes: int
    daylight_saving: DaylightSaving = field(default_factory=DaylightSaving)


# SNMPv3 enums used by the controller UI (Config -> Services -> SNMP).
SNMP_SECURITY_LEVELS = {0: "noAuthNoPriv", 1: "authNoPriv", 2: "authPriv"}
SNMP_AUTH_MODES = {1: "MD5", 2: "SHA"}
SNMP_PRIVACY_MODES = {1: "DES", 2: "AES"}


@dataclass(frozen=True)
class SnmpConfig:
    """SET key ``snmp``: SNMP agent settings (read-only access, as on an EAP)."""

    location: str = ""
    contact: str = ""
    v1v2c_enabled: bool = False
    community: SecretValue | None = field(default=None, repr=False)
    v3_enabled: bool = False
    v3_username: str = ""
    v3_security_level: int = 1
    v3_auth_mode: int = 1
    v3_password: SecretValue | None = field(default=None, repr=False)
    v3_privacy_mode: int = 1
    v3_privacy_password: SecretValue | None = field(default=None, repr=False)

    @property
    def enabled(self) -> bool:
        return self.v1v2c_enabled or self.v3_enabled


@dataclass(frozen=True)
class LedConfig:
    enabled: bool | None = None
    locate: bool | None = None
    raw: Mapping[str, Any] = field(default_factory=dict)

@dataclass(frozen=True)
class WifiControlLedConfig:
    enabled: bool | None = None
    is_pressed: bool | None = None
    raw: Mapping[str, Any] = field(default_factory=dict)

class DeviceProfile(Protocol):
    def identity(self) -> DeviceIdentity: ...
    def device_info(self) -> Mapping[str, Any]: ...
    def device_misc(self) -> Mapping[str, Any]: ...
    def components_v2(self) -> Mapping[str, str]: ...
    def channel_info(self) -> tuple[Mapping[str, Any], ...]: ...
    def radio_cap(self) -> tuple[Mapping[str, Any], ...]: ...
    def dev_cap(self) -> Mapping[str, Any]: ...
