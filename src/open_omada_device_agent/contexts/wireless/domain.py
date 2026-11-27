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

# 802.11 Rate Control (Observed: lab, Controller 6.3.0.111). Each SSID item
# carries ``rateCtl`` and ``manageRateCtl``; 0 means off. Bits 0-11 are the
# allowed legacy rates (kb/s, in this order), bit 16 turns the control on.
# ``rateCtl`` bit 17 = "Require Clients to Use Rates at or Above the Minimum
# Rate", bit 18 = "Send Beacons at 1 Mbps". ``manageRateCtl`` carries one
# rate bit: the management (basic) rate.
RATE_CONTROL_RATES = (1000, 2000, 5500, 11000, 6000, 9000, 12000, 18000, 24000, 36000, 48000, 54000)
RATE_CONTROL_ENABLE = 1 << 16
RATE_CONTROL_REQUIRE = 1 << 17
RATE_CONTROL_BEACON_1M = 1 << 18
_RATE_BITS = (1 << len(RATE_CONTROL_RATES)) - 1
CCK_RATES = frozenset({1000, 2000, 5500, 11000})


@dataclass(frozen=True)
class RateControl:
    """One SSID's 802.11 Rate Control; ``enabled`` False means off."""
    enabled: bool = False
    rates: tuple[int, ...] = ()  # kb/s, ascending
    require_minimum: bool = False
    beacon_1mbps: bool = False
    management_rate: int | None = None  # kb/s
    # Bits the agent does not know (Unknown); a non-zero value is refused.
    unknown_bits: int = 0

    @classmethod
    def from_codes(cls, rate_ctl: int, manage_rate_ctl: int = 0) -> "RateControl":
        known = _RATE_BITS | RATE_CONTROL_ENABLE | RATE_CONTROL_REQUIRE | RATE_CONTROL_BEACON_1M
        unknown = (rate_ctl & ~known) | (manage_rate_ctl & ~(_RATE_BITS | RATE_CONTROL_ENABLE))
        management = _rates(manage_rate_ctl) if manage_rate_ctl & RATE_CONTROL_ENABLE else ()
        return cls(
            enabled=bool(rate_ctl & RATE_CONTROL_ENABLE),
            rates=_rates(rate_ctl),
            require_minimum=bool(rate_ctl & RATE_CONTROL_REQUIRE),
            beacon_1mbps=bool(rate_ctl & RATE_CONTROL_BEACON_1M),
            management_rate=management[0] if management else None,
            unknown_bits=unknown,
        )


def _rates(code: int) -> tuple[int, ...]:
    return tuple(sorted(rate for bit, rate in enumerate(RATE_CONTROL_RATES) if code >> bit & 1))


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
    # SSID "Load Balance" (SET ``loadBalance``): 802.11k/v client steering.
    load_balance: bool | None = None
    # 802.11 Rate Control (SET ``rateCtl`` / ``manageRateCtl``).
    rate_control: RateControl | None = None
    # The SSID's raw controller data holds the Wi-Fi key: keep it out of repr.
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False)

def validate_ssid_name(name: str) -> str:
    return Ssid(name).value


# MAC filtering (Observed, lab, Controller 6.3.0.111). The controller sends
# changes as rules: ``operation`` 1 = add, 2 = delete, 3 = update. A full sync
# (``configVersion``) resends every rule with operation 1.
MAC_FILTER_ADD = 1
MAC_FILTER_DELETE = 2
MAC_FILTER_UPDATE = 3
MAC_FILTER_OPERATIONS = (MAC_FILTER_ADD, MAC_FILTER_DELETE, MAC_FILTER_UPDATE)
# macFilterAssoc ``action``: 0 = Deny List, 1 = Allow List; ``type`` 0 = MAC
# group (type 1, an OUI profile, is Inferred and has no hostapd equivalent).
MAC_FILTER_DENY = 0
MAC_FILTER_ALLOW = 1
MAC_FILTER_TYPE_GROUP = 0
# ``listId`` -1 in a delete means every list on that SSID and band.
MAC_FILTER_ANY_LIST = -1


@dataclass(frozen=True)
class MacFilterListRule:
    """One ``macFilterList`` rule: a MAC group (no ``mac``) or one member."""
    operation: int
    list_id: int
    rule_id: int | None = None
    mac: str | None = None


@dataclass(frozen=True)
class MacFilterAssocRule:
    """One ``macFilterAssoc`` rule: which group filters an SSID on a band."""
    operation: int
    ssid_id: int
    band: int
    list_id: int
    action: int
    type: int = MAC_FILTER_TYPE_GROUP


@dataclass(frozen=True)
class MacFilterChange:
    enabled: bool | None = None
    list_rules: tuple[MacFilterListRule, ...] = ()
    assoc_rules: tuple[MacFilterAssocRule, ...] = ()


@dataclass(frozen=True)
class MacFilterPolicy:
    """What one SSID on one band should enforce."""
    allow: bool
    macs: tuple[str, ...]


@dataclass(frozen=True)
class MacFilterState:
    """The controller's MAC filter setup as the agent last applied it."""
    enabled: bool = False
    # list_id -> {rule_id: mac}
    lists: Mapping[int, Mapping[int, str]] = field(default_factory=dict)
    # (ssid_id, band) -> {list_id: (action, type)}
    assoc: Mapping[tuple[int, int], Mapping[int, tuple[int, int]]] = field(default_factory=dict)

    def apply(self, change: MacFilterChange, *, full_sync: bool) -> "MacFilterState":
        enabled = self.enabled if not full_sync else False
        lists = {} if full_sync else {key: dict(value) for key, value in self.lists.items()}
        assoc = {} if full_sync else {key: dict(value) for key, value in self.assoc.items()}
        if change.enabled is not None:
            enabled = change.enabled
        for rule in change.list_rules:
            if rule.rule_id is None:
                # The group itself.
                if rule.operation == MAC_FILTER_DELETE:
                    lists.pop(rule.list_id, None)
                else:
                    lists.setdefault(rule.list_id, {})
                continue
            members = lists.setdefault(rule.list_id, {})
            if rule.operation == MAC_FILTER_DELETE:
                members.pop(rule.rule_id, None)
            elif rule.mac is not None:
                members[rule.rule_id] = rule.mac
        for rule in change.assoc_rules:
            key = (rule.ssid_id, rule.band)
            links = assoc.setdefault(key, {})
            if rule.operation == MAC_FILTER_DELETE:
                if rule.list_id == MAC_FILTER_ANY_LIST:
                    links.clear()
                else:
                    links.pop(rule.list_id, None)
            else:
                links[rule.list_id] = (rule.action, rule.type)
            if not links:
                assoc.pop(key, None)
        return MacFilterState(enabled=enabled, lists=lists, assoc=assoc)

    def policy(self, ssid_id: int | None, band: int) -> MacFilterPolicy | None:
        """The filter for one SSID and band, or None when it has none."""
        if not self.enabled or ssid_id is None:
            return None
        # A link to a group that no longer exists filters nothing; without
        # this a deleted group could leave an empty Allow List that blocks
        # every client.
        links = {
            list_id: action
            for list_id, (action, _) in self.assoc.get((ssid_id, band), {}).items()
            if list_id in self.lists
        }
        if not links:
            return None
        # With any Allow List linked, only allowed MACs may join, so Deny
        # Lists add nothing (Inferred: the controller UI offers one mode per
        # SSID). An Allow List with an empty group blocks every client
        # (Unknown whether a real EAP does the same).
        allow = MAC_FILTER_ALLOW in links.values()
        wanted = MAC_FILTER_ALLOW if allow else MAC_FILTER_DENY
        macs = sorted(
            {
                mac
                for list_id, action in links.items()
                if action == wanted
                for mac in self.lists[list_id].values()
            }
        )
        return MacFilterPolicy(allow=allow, macs=tuple(macs))

    def to_dict(self) -> dict[str, Any]:
        """A JSON-safe copy, for saving between agent restarts."""
        return {
            "enabled": self.enabled,
            "lists": {str(list_id): {str(rule): mac for rule, mac in members.items()}
                      for list_id, members in self.lists.items()},
            "assoc": [
                {"ssidId": ssid_id, "band": band, "listId": list_id, "action": action, "type": kind}
                for (ssid_id, band), links in self.assoc.items()
                for list_id, (action, kind) in links.items()
            ],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "MacFilterState":
        """Rebuild a state saved by ``to_dict``; raises on a malformed file."""
        lists = {
            int(list_id): {int(rule): str(mac) for rule, mac in members.items()}
            for list_id, members in data.get("lists", {}).items()
        }
        assoc: dict[tuple[int, int], dict[int, tuple[int, int]]] = {}
        for link in data.get("assoc", []):
            key = (int(link["ssidId"]), int(link["band"]))
            assoc.setdefault(key, {})[int(link["listId"])] = (int(link["action"]), int(link["type"]))
        return cls(enabled=bool(data.get("enabled", False)), lists=lists, assoc=assoc)
