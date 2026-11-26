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
