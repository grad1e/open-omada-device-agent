"""Parse Omada AP SET_REQUEST bodies into platform-independent domain updates."""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from .....application.commands import ApplyDeviceConfigurationCommand
from .....contexts.clients.domain import (
    ClientAuthConfig, ClientControlOperation, ClientRateConfig, ClientRateLimit,
)
from .....contexts.device.domain import (
    MAX_UTC_OFFSET_MINUTES, MIN_UTC_OFFSET_MINUTES, TIME_ZONE_BASE, DaylightSaving,
    LedConfig, LldpConfig, NetworkCheckRequest, SnmpConfig, TimeZoneSetting, WifiControlLedConfig,
)
from .....contexts.networking.domain import LanIpSetting, ManagementVlan, validate_vlan_id
from .....contexts.portal.domain import PortalConfiguration, PortalFreePolicy
from .....contexts.wireless.domain import (
    MacFilterAssocRule, MacFilterChange, MacFilterListRule,
    CaptivePortalIntent, ClientLimit, RadioAdvancedConfig, RadioBand, RadioConfig, RateControl,
    WirelessDhcpOption82Intent,
    WirelessNetwork, WirelessSecurity, WirelessVlanIntent, validate_ssid_name,
)
from .....contexts.wireless.schedule import (
    ScheduleAssocRule, ScheduleChange, ScheduleListRule, SchedulePeriod,
)
from .....shared.domain import SecretValue

AccessPointConfigUpdate = ApplyDeviceConfigurationCommand

RADIO_KEYS: dict[str, RadioBand] = {
    "wirelessBasic_2G": RadioBand.TWO_G,
    "wirelessBasic_5G": RadioBand.FIVE_G,
    "wirelessBasic_5G2": RadioBand.FIVE_G2,
    "wirelessBasic_6G": RadioBand.SIX_G,
}

BAND_SUFFIXES: dict[str, RadioBand] = {
    "2G": RadioBand.TWO_G,
    "5G": RadioBand.FIVE_G,
    "5G2": RadioBand.FIVE_G2,
    "6G": RadioBand.SIX_G,
}
# Per-radio Advanced settings (Controller 6.3.0.111 components lb, rssiFilter,
# qos and wlanAdv): Maximum Associated Clients, RSSI Threshold, U-APSD, OFDMA.
RADIO_ADVANCED_PREFIXES = ("loadBalance_", "rssi_", "qosConfig_", "wirelessAdv_")
RADIO_ADVANCED_KEYS = {
    f"{prefix}{suffix}" for prefix in RADIO_ADVANCED_PREFIXES for suffix in BAND_SUFFIXES
}

SSID_KEYS: dict[str, RadioBand] = {
    "ssid_2G": RadioBand.TWO_G,
    "ssid_5G": RadioBand.FIVE_G,
    "ssid_5G2": RadioBand.FIVE_G2,
    "ssid_6G": RadioBand.SIX_G,
}

KNOWN_COMMON_KEYS = {"sequenceId", "configVersion", "configVersionInc"}
# Controller-level defaults that are part of ordinary Omada AP config pushes but
# do not currently require a direct OpenWrt side effect for the supported AP
# runtime to stay correct.
CONTROL_PLANE_PASSIVE_CONFIG_KEYS = {
    # Site Settings save sends {"logoutDomain": "portal.tplink.net"} (portal
    # logout domain); not applied to openNDS yet.
    "controllerSetting",
    "ipGroup",
    "ipv6Group",
    "logSetting",
    "ssh",
}

# These AP config domains are accepted as known but currently have no local
# OpenWrt side effect. They should not block the actionable SSID/radio domains
# carried by the same SET_REQUEST.
PASSIVE_CONFIG_KEYS = {
    *CONTROL_PLANE_PASSIVE_CONFIG_KEYS,
    # Rogue AP detection switch and the trusted-AP list: the controller does
    # the classification itself from ``rogueApList``.
    "rogueApGlobal",
    "rogueApSetting",
}

ACK_ONLY_CONFIG_KEYS: set[str] = set()

# Tools -> Network Check: one SET key per tool.
NETWORK_CHECK_KEYS = ("ping", "traceroute", "dnslookup", "arptable")

# MAC filtering: the global switch, the MAC groups and their SSID links.
MAC_FILTER_KEYS = ("macFilterGlobal", "macFilterList", "macFilterAssoc")

# WLAN schedules: the global switch, the time ranges and their SSID links.
SCHEDULE_KEYS = ("schedulerGlobal", "schedulerList", "schedulerAssoc")

KNOWN_CONFIG_KEYS = set(RADIO_KEYS) | set(SSID_KEYS) | RADIO_ADVANCED_KEYS | PASSIVE_CONFIG_KEYS | {
    "clientConfig",
    "clientOperation",
    "clientOperation_cmd",
    "clientRateConfig",
    "lanSetting",
    *MAC_FILTER_KEYS,
    *SCHEDULE_KEYS,
    "led",
    "managementVlan",
    "portalConfigList",
    "portalFreePolicyConfig",
    "rogueApScan",
    "dst",
    "lldp",
    "snmp",
    "timeSetting",
    "wifiControlLed",
} | set(NETWORK_CHECK_KEYS)


def parse_set_request(message: Mapping[str, Any]) -> AccessPointConfigUpdate:
    body = message.get("body")
    if not isinstance(body, Mapping):
        raise ValueError("SET_REQUEST body must be a JSON object")
    return parse_config_body(body)


def parse_config_body(body: Mapping[str, Any]) -> AccessPointConfigUpdate:
    radios = []
    wlans = []
    wlan_bands = []

    for key, band in RADIO_KEYS.items():
        raw = body.get(key)
        if raw is not None:
            radios.append(_parse_radio_config(band, raw))

    for key, band in SSID_KEYS.items():
        raw = body.get(key)
        if raw is not None:
            wlans.extend(_parse_ssid_config(band, raw))
            wlan_bands.append(band)

    radio_advanced = _parse_radio_advanced(body)

    management_vlan = None
    raw_management_vlan = body.get("managementVlan")
    if raw_management_vlan is not None:
        management_vlan = _parse_management_vlan(raw_management_vlan)

    portal_free_policy = None
    raw_portal_free_policy = body.get("portalFreePolicyConfig")
    if raw_portal_free_policy is not None:
        portal_free_policy = _parse_portal_free_policy(raw_portal_free_policy)

    portal_configs: tuple[PortalConfiguration, ...] = ()
    raw_portal_configs = body.get("portalConfigList")
    if raw_portal_configs is not None:
        portal_configs = _parse_portal_configs(raw_portal_configs)

    led = None
    raw_led = body.get("led")
    if raw_led is not None:
        led = _parse_led(raw_led)

    wifi_control_led = None
    raw_wifi_control_led = body.get("wifiControlLed")
    if raw_wifi_control_led is not None:
        wifi_control_led = _parse_wifi_control_led(raw_wifi_control_led)

    client_configs: tuple[ClientAuthConfig, ...] = ()
    raw_client_config = body.get("clientConfig")
    if raw_client_config is not None:
        client_configs = _parse_client_config(raw_client_config)

    client_operations: list[ClientControlOperation] = []
    for source_key in ("clientOperation", "clientOperation_cmd"):
        raw_client_operation = body.get(source_key)
        if raw_client_operation is not None:
            client_operations.extend(
                _parse_client_operations(raw_client_operation, source_key=source_key)
            )

    client_rate_config = None
    raw_client_rate_config = body.get("clientRateConfig")
    if raw_client_rate_config is not None:
        client_rate_config = _parse_client_rate_config(raw_client_rate_config)

    unhandled = tuple(
        sorted(
            key
            for key in body
            if key not in KNOWN_COMMON_KEYS and key not in KNOWN_CONFIG_KEYS
        )
    )
    return AccessPointConfigUpdate(
        sequence_id=_optional_int(body.get("sequenceId")),
        config_version=_optional_int(body.get("configVersion")),
        config_version_inc=_optional_int(body.get("configVersionInc")),
        radios=tuple(radios),
        wlans=tuple(wlans),
        wlan_bands=tuple(wlan_bands),
        management_vlan=management_vlan,
        portal_free_policy=portal_free_policy,
        portal_configs=portal_configs,
        led=led,
        wifi_control_led=wifi_control_led,
        client_configs=client_configs,
        client_operations=tuple(client_operations),
        client_rate_config=client_rate_config,
        rogue_ap_scan=body.get("rogueApScan") is not None,
        network_checks=_parse_network_checks(body),
        snmp=_parse_snmp(body.get("snmp")),
        lldp=_parse_lldp(body.get("lldp")),
        time_zone=parse_time_zone(body),
        lan_ip=_parse_lan_setting(body.get("lanSetting")),
        mac_filter=_parse_mac_filter(body),
        schedule=_parse_schedule(body),
        radio_advanced=radio_advanced,
        passive_keys=tuple(sorted(key for key in body if key in PASSIVE_CONFIG_KEYS)),
        ack_only_keys=tuple(sorted(key for key in body if key in ACK_ONLY_CONFIG_KEYS)),
        unhandled_keys=unhandled,
        raw_body=dict(body),
    )



LAN_CONN_TYPES = {0: "static", 1: "dhcp"}


def _parse_lan_setting(raw: Any) -> LanIpSetting | None:
    if raw is None:
        return None
    data = _require_mapping(raw, "lanSetting")
    conn_type = _optional_int(data.get("connType"))
    if conn_type not in LAN_CONN_TYPES:
        # Unknown or missing mode: keep the AP's address as it is.
        return None

    def text(key: str) -> str | None:
        value = data.get(key)
        value = "" if value is None else str(value).strip()
        return value or None

    return LanIpSetting(
        mode=LAN_CONN_TYPES[conn_type],
        ip_address=text("ipAddr"),
        netmask=text("netMask"),
        gateway=text("gateway"),
        dns=tuple(v for v in (text("preferredDNS"), text("alternateDNS")) if v),
        fallback_enabled=bool(_optional_bool(data.get("useFallBack"))),
        fallback_ip=text("fallBackIp"),
        fallback_netmask=text("fallBackMask"),
        fallback_gateway=text("fallbackGateway"),
    )


# Unix times up to 2106 and a daylight-saving shift of at most 2 hours.
MAX_DST_TIME = 2**32 - 1
MAX_DST_OFFSET = 7200


def parse_time_zone(body: Mapping[str, Any]) -> TimeZoneSetting | None:
    """``timeSetting.timeZone`` (offset + 720) and ``dst``; None if unusable."""
    raw = body.get("timeSetting")
    if not isinstance(raw, Mapping):
        return None
    try:
        code = _optional_int(raw.get("timeZone"))
    except (TypeError, ValueError):
        return None
    if code is None:
        return None
    offset = code - TIME_ZONE_BASE
    if not MIN_UTC_OFFSET_MINUTES <= offset <= MAX_UTC_OFFSET_MINUTES or offset % 15:
        return None
    return TimeZoneSetting(utc_offset_minutes=offset, daylight_saving=_parse_dst(body.get("dst")))


def _parse_dst(raw: Any) -> DaylightSaving | None:
    """``dst``; None if absent, off (all zero) if malformed or out of range."""
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        return DaylightSaving()
    try:
        dst = DaylightSaving(
            enabled=bool(_optional_bool(raw.get("enable"))),
            start=_optional_int(raw.get("start")) or 0,
            end=_optional_int(raw.get("end")) or 0,
            offset=_optional_int(raw.get("offset")) or 0,
        )
    except (TypeError, ValueError):
        return DaylightSaving()
    if not (0 <= dst.start <= MAX_DST_TIME and 0 <= dst.end <= MAX_DST_TIME and 0 <= dst.offset <= MAX_DST_OFFSET):
        return DaylightSaving()
    return dst


def _parse_lldp(raw: Any) -> LldpConfig | None:
    if raw is None:
        return None
    data = _require_mapping(raw, "lldp")
    return LldpConfig(enabled=bool(_optional_bool(data.get("enable"))))


def _parse_snmp(raw: Any) -> SnmpConfig | None:
    if raw is None:
        return None
    data = _require_mapping(raw, "snmp")

    def text(key: str) -> str:
        value = data.get(key)
        return "" if value is None else str(value)

    def secret(key: str) -> SecretValue | None:
        value = text(key)
        return SecretValue(value) if value else None

    def number(key: str, default: int) -> int:
        value = _optional_int(data.get(key))
        return default if value is None else value

    return SnmpConfig(
        location=text("location"),
        contact=text("contact"),
        v1v2c_enabled=bool(_optional_bool(data.get("v1v2cEnable"))),
        community=secret("community"),
        v3_enabled=bool(_optional_bool(data.get("v3Enable"))),
        v3_username=text("v3Username"),
        v3_security_level=number("v3SecurityLevel", 1),
        v3_auth_mode=number("v3AuthMode", 1),
        v3_password=secret("v3Password"),
        v3_privacy_mode=number("v3PrivacyMode", 1),
        v3_privacy_password=secret("v3PrivacyPassword"),
    )


def _parse_network_checks(body: Mapping[str, Any]) -> tuple[NetworkCheckRequest, ...]:
    requests = []
    for kind in NETWORK_CHECK_KEYS:
        raw = body.get(kind)
        if raw is None:
            continue
        data = _require_mapping(raw, kind)
        additional = data.get("additional")
        additional = additional if isinstance(additional, Mapping) else {}
        dest = data.get("dest")
        requests.append(
            NetworkCheckRequest(
                kind=kind,
                cmd_id=str(data.get("cmdId") or ""),
                dest=str(dest).strip() if dest not in (None, "") else None,
                count=_optional_int(additional.get("cnt")),
                packet_size=_optional_int(additional.get("pktSize")),
            )
        )
    return tuple(requests)


def _parse_radio_config(band: RadioBand, raw: Any) -> RadioConfig:
    data = _require_mapping(raw, f"{band.value} radio config")
    return RadioConfig(
        band=band,
        radio_id=_optional_int(data.get("radioId")),
        enabled=_optional_bool(data.get("radioEnable")),
        channel_width=_optional_int(data.get("chanWidth")),
        channel=_optional_int(data.get("channel")),
        channel_range=_frequency_list(data.get("channelRange")),
        tx_power=_optional_int(data.get("txPower")),
        channel_limit=_optional_bool(data.get("channelLimit")),
        wireless_mode=_optional_int(data.get("wirelessMode")),
        raw=dict(data),
    )


def _frequency_list(raw: Any) -> tuple[int, ...]:
    if not isinstance(raw, (list, tuple)):
        return ()
    values = []
    for item in raw:
        value = _optional_int(item)
        if value is not None and value > 0:
            values.append(value)
    return tuple(values)


def _parse_ssid_config(band: RadioBand, raw: Any) -> tuple[WirelessNetwork, ...]:
    data = _require_mapping(raw, f"{band.value} SSID config")
    radio_id = _optional_int(data.get("radioId"))
    ssid_items = data.get("ssid", ())
    return tuple(
        _parse_ssid_item(band, radio_id, item)
        for item in _iter_ssid_items(ssid_items)
    )


def _parse_ssid_item(
    band: RadioBand,
    radio_id: int | None,
    raw: Any,
) -> WirelessNetwork:
    data = _require_mapping(raw, f"{band.value} SSID item")
    name = validate_ssid_name(str(data.get("ssidName") or ""))
    vlan_id = _optional_vlan_id(data.get("vlanId"))
    fast_transition = data.get("fastTransition")
    return WirelessNetwork(
        band=band,
        radio_id=radio_id,
        ssid_id=_optional_int(data.get("id")),
        index=_optional_int(data.get("index")),
        operation=_optional_int(data.get("operation")),
        name=name,
        broadcast=_optional_bool(data.get("ssidBcast")),
        client_isolation=_optional_bool(data.get("ssidIsolation")),
        load_balance=_optional_bool(data.get("loadBalance")),
        rate_control=(
            RateControl.from_codes(int(data["rateCtl"]), _optional_int(data.get("manageRateCtl")) or 0)
            if data.get("rateCtl") is not None
            else None
        ),
        vlan=WirelessVlanIntent(
            vlan_id=vlan_id,
            vlan_pool_ids=tuple(str(value) for value in data.get("vlanPoolIds") or ()),
            dynamic_vlan_mode=_optional_int(data.get("dyVlanMode")),
            dhcp_option82=_parse_dhcp_option82(data.get("dhcpOp82")),
        ),
        security=WirelessSecurity(
            security_mode=_optional_int(data.get("securityMode")),
            auth_type=_optional_int(data.get("authType")),
            wpa_version=_optional_int(data.get("wpaVer")),
            wpa_cipher=_optional_int(data.get("wpaCipher")),
            psk_version=_optional_int(data.get("pskVer")),
            psk_cipher=_optional_int(data.get("pskCipher")),
            psk_configured=bool(data.get("pskKey")),
            psk_key=(
                SecretValue(str(data["pskKey"]))
                if data.get("pskKey") is not None
                else None
            ),
            radius_profile_id=_optional_str(data.get("wpaRadiusProfileId")),
            radius_auth=_optional_active_mapping(data.get("radiusAuth")),
            radius_accounting=_optional_active_mapping(data.get("radiusAccounting")),
            radius_mac_auth=_optional_active_mapping(data.get("macAuth")),
            pmf_mode=_optional_int(data.get("pmfMode")),
            fast_roaming=(
                _optional_bool(fast_transition.get("enable11r"))
                if isinstance(fast_transition, Mapping)
                else None
            ),
            ft_mobility_domain=(
                _optional_str(fast_transition.get("mobilityDomain"))
                if isinstance(fast_transition, Mapping)
                else None
            ),
            ft_key=(
                SecretValue(str(fast_transition["keyRrb"]))
                if isinstance(fast_transition, Mapping) and fast_transition.get("keyRrb")
                else None
            ),
            raw=dict(data),
        ),
        portal=CaptivePortalIntent(
            enabled=bool(data.get("portal")),
            https_redirect=_optional_bool(data.get("httpsRedirectEnable")),
            hotspot_v2=_optional_mapping(data.get("hotspotV2")),
            raw=dict(data),
        ),
        raw=dict(data),
    )


def _parse_dhcp_option82(raw: Any) -> WirelessDhcpOption82Intent | None:
    if raw is None:
        return None
    data = _require_mapping(raw, "DHCP option 82 config")
    return WirelessDhcpOption82Intent(
        enabled=bool(data.get("option82En")),
        format=_optional_int(data.get("option82Format")),
        delimiter=_optional_str(data.get("delimiter")),
        circuit_id=tuple(int(value) for value in data.get("circuitId") or ()),
        remote_id=tuple(int(value) for value in data.get("remoteId") or ()),
        site_name=_optional_str(data.get("siteName")),
        raw=dict(data),
    )


def _parse_radio_advanced(body: Mapping[str, Any]) -> tuple[RadioAdvancedConfig, ...]:
    configs: list[RadioAdvancedConfig] = []
    for suffix, band in BAND_SUFFIXES.items():
        raw = {prefix: body.get(f"{prefix}{suffix}") for prefix in RADIO_ADVANCED_PREFIXES}
        if all(value is None for value in raw.values()):
            continue
        load_balance = _optional_mapping(raw["loadBalance_"]) or {}
        rssi = _optional_mapping(raw["rssi_"]) or {}
        qos = _optional_mapping(raw["qosConfig_"]) or {}
        advanced = _optional_mapping(raw["wirelessAdv_"]) or {}
        radio_id = next(
            (
                _optional_int(section.get("radioId"))
                for section in (load_balance, qos, advanced)
                if section.get("radioId") is not None
            ),
            None,
        )
        configs.append(
            RadioAdvancedConfig(
                band=band,
                radio_id=radio_id,
                max_clients=_client_limit(load_balance, "maxAssocClient"),
                rssi_threshold=_client_limit(rssi, "threshold"),
                uapsd=_optional_bool(qos.get("uapsdEnable")),
                ofdma=_optional_bool(advanced.get("ofdma")),
            )
        )
    return tuple(configs)


def _client_limit(data: Mapping[str, Any], value_key: str) -> ClientLimit | None:
    if "enable" not in data:
        return None
    enabled = bool(_optional_bool(data.get("enable")))
    return ClientLimit(enabled=enabled, value=_optional_int(data.get(value_key)))


def _parse_management_vlan(raw: Any) -> ManagementVlan:
    data = _require_mapping(raw, "management VLAN config")
    enabled = _enabled_string(data.get("managementVlanEnable"))
    vlan_id = _optional_vlan_id(data.get("managementVlanId"))
    return ManagementVlan(enabled=enabled, vlan_id=vlan_id, raw=dict(data))


def _parse_portal_free_policy(raw: Any) -> PortalFreePolicy:
    data = _require_mapping(raw, "portal free policy config")
    return PortalFreePolicy(
        layer2_rules=tuple(
            _require_mapping(item, "portal free policy item")
            for item in data.get("portalFreePolicy") or ()
        ),
        url_rules=tuple(
            _require_mapping(item, "portal URL free policy item")
            for item in data.get("urlPortalFreePolicy") or ()
        ),
        raw=dict(data),
    )


def _parse_portal_configs(raw: Any) -> tuple[PortalConfiguration, ...]:
    return tuple(
        _parse_portal_config_item(item)
        for item in _iter_items(raw, "portalConfigList")
    )


def _parse_portal_config_item(raw: Any) -> PortalConfiguration:
    data = _require_mapping(raw, "portalConfigList item")
    return PortalConfiguration(
        auth_type=_optional_int(data.get("authType")),
        auth_timeout=_optional_int(data.get("authTimeout")),
        portal_day=_optional_int(data.get("portalDay")),
        portal_hour=_optional_int(data.get("portalHour")),
        portal_min=_optional_int(data.get("portalMin")),
        https_redirect_enable=_optional_bool(data.get("httpsRedirectEnable")),
        redirect=_optional_bool(data.get("redirect")),
        redirect_url=_optional_str(data.get("redirectUrl")),
        auth_server_type=_optional_int(data.get("authServerType")),
        ext_auth_server=_optional_str(data.get("extAuthServer")),
        external_portal_server=_optional_str(data.get("externalPortalServer")),
        site_id=_optional_str(data.get("siteId")),
        site_name=_optional_str(data.get("siteName") or data.get("site")),
        portal_title=_optional_str(data.get("portalTitle")),
        portal_accept=_optional_bool(data.get("portalAccept")),
        ssid_list=tuple(str(value) for value in data.get("ssidList") or ()),
        raw=_redact_portal_config(data),
    )


def _redact_portal_config(data: Mapping[str, Any]) -> dict[str, Any]:
    redacted = dict(data)
    for key in ("password", "radiusPassword"):
        if key in redacted:
            redacted[key] = "***"
    return redacted


def _parse_led(raw: Any) -> LedConfig:
    data = _require_mapping(raw, "LED config")
    return LedConfig(
        enabled=_optional_enabled(data.get("enable")),
        locate=_optional_bool(data.get("locate")),
        raw=dict(data),
    )


def _parse_wifi_control_led(raw: Any) -> WifiControlLedConfig:
    data = _require_mapping(raw, "WiFi control LED config")
    return WifiControlLedConfig(
        enabled=_optional_enabled(data.get("enable")),
        is_pressed=_optional_bool(data.get("isPressed")),
        raw=dict(data),
    )


def _parse_client_config(raw: Any) -> tuple[ClientAuthConfig, ...]:
    return tuple(_parse_client_config_item(item) for item in _iter_items(raw, "clientConfig"))


def _parse_client_config_item(raw: Any) -> ClientAuthConfig:
    data = _require_mapping(raw, "clientConfig item")
    return ClientAuthConfig(
        client_mac=_required_str(data.get("clientMac"), "clientConfig.clientMac"),
        unauthenticated=_optional_bool(data.get("unauth")),
        raw=dict(data),
    )


def _parse_client_operations(
    raw: Any,
    *,
    source_key: str,
) -> tuple[ClientControlOperation, ...]:
    return tuple(
        _parse_client_operation_item(item, source_key=source_key)
        for item in _iter_items(raw, source_key)
    )


def _parse_client_operation_item(
    raw: Any,
    *,
    source_key: str,
) -> ClientControlOperation:
    data = _require_mapping(raw, f"{source_key} item")
    return ClientControlOperation(
        client_mac=_required_str(data.get("clientMac"), f"{source_key}.clientMac"),
        operation=_optional_int(data.get("operation")),
        ssid=_optional_str(data.get("ssid")),
        radio_id=_optional_int(data.get("radioId")),
        vid=_optional_int(data.get("vid")),
        port=_optional_int(data.get("port")),
        wireless=_optional_bool(data.get("wireless")),
        source_key=source_key,
        raw=dict(data),
    )


def _parse_client_rate_config(raw: Any) -> ClientRateConfig:
    data = _require_mapping(raw, "clientRateConfig")
    limits = tuple(
        _parse_client_rate_limit(item)
        for item in _iter_items(data.get("clientRateLimit") or (), "clientRateLimit")
    )
    return ClientRateConfig(
        action=_optional_int(data.get("action")),
        limits=limits,
        raw=dict(data),
    )


def _parse_client_rate_limit(raw: Any) -> ClientRateLimit:
    data = _require_mapping(raw, "clientRateLimit item")
    return ClientRateLimit(
        mac=_required_str(data.get("mac"), "clientRateLimit.mac"),
        down=_optional_int(data.get("down")),
        up=_optional_int(data.get("up")),
        raw=dict(data),
    )


def _iter_ssid_items(raw: Any) -> Iterable[Any]:
    return _iter_items(raw, "SSID list")


def _iter_items(raw: Any, label: str) -> Iterable[Any]:
    if raw is None:
        return ()
    if isinstance(raw, Mapping):
        return (raw,)
    if isinstance(raw, Iterable) and not isinstance(raw, (str, bytes, bytearray)):
        return raw
    raise ValueError(f"{label} must be an object or array")


def _parse_mac_filter(body: Mapping[str, Any]) -> MacFilterChange | None:
    if not any(key in body for key in MAC_FILTER_KEYS):
        return None
    global_raw = _optional_mapping(body.get("macFilterGlobal")) or {}
    list_rules = []
    for item in _mac_filter_rules(body.get("macFilterList"), "macFilterList"):
        list_rules.append(
            MacFilterListRule(
                operation=_required_int(item, "operation", "macFilterList"),
                list_id=_required_int(item, "listId", "macFilterList"),
                rule_id=_optional_int(item.get("ruleId")),
                mac=str(item["mac"]) if item.get("mac") is not None else None,
            )
        )
    assoc_rules = []
    for item in _mac_filter_rules(body.get("macFilterAssoc"), "macFilterAssoc"):
        assoc_rules.append(
            MacFilterAssocRule(
                operation=_required_int(item, "operation", "macFilterAssoc"),
                ssid_id=_required_int(item, "ssidId", "macFilterAssoc"),
                band=_required_int(item, "band", "macFilterAssoc"),
                list_id=_required_int(item, "listId", "macFilterAssoc"),
                action=_optional_int(item.get("action")) or 0,
                type=_optional_int(item.get("type")) or 0,
            )
        )
    return MacFilterChange(
        enabled=_optional_bool(global_raw.get("enable")),
        list_rules=tuple(list_rules),
        assoc_rules=tuple(assoc_rules),
    )


def _parse_schedule(body: Mapping[str, Any]) -> ScheduleChange | None:
    if not any(key in body for key in SCHEDULE_KEYS):
        return None
    global_raw = _optional_mapping(body.get("schedulerGlobal")) or {}
    list_rules = []
    for item in _mac_filter_rules(body.get("schedulerList"), "schedulerList"):
        rule_id = _optional_int(item.get("ruleId"))
        period = None
        if rule_id is not None and item.get("type") is not None:
            period = SchedulePeriod(
                type=str(item["type"]),
                day=_optional_int(item.get("day")) or 0,
                start=(_optional_int(item.get("startH")) or 0) * 60 + (_optional_int(item.get("startM")) or 0),
                end=(_optional_int(item.get("endH")) or 0) * 60 + (_optional_int(item.get("endM")) or 0),
            )
        list_rules.append(
            ScheduleListRule(
                operation=_required_int(item, "operation", "schedulerList"),
                profile_id=_required_int(item, "profileId", "schedulerList"),
                rule_id=rule_id,
                period=period,
            )
        )
    assoc_rules = []
    for item in _mac_filter_rules(body.get("schedulerAssoc"), "schedulerAssoc"):
        assoc_rules.append(
            ScheduleAssocRule(
                ssid_id=_required_int(item, "ssidId", "schedulerAssoc"),
                band=_required_int(item, "band", "schedulerAssoc"),
                profile_id=_optional_int(item.get("profileId")),
                action=_optional_int(item.get("action")) or 0,
            )
        )
    return ScheduleChange(
        enabled=_optional_bool(global_raw.get("enable")),
        mode=_optional_int(global_raw.get("mode")),
        list_rules=tuple(list_rules),
        assoc_rules=tuple(assoc_rules),
    )


def _mac_filter_rules(raw: Any, label: str) -> list[Mapping[str, Any]]:
    if raw is None:
        return []
    rules = _require_mapping(raw, label).get("rule") or []
    if not isinstance(rules, list):
        raise ValueError(f"{label}.rule must be a list")
    return [_require_mapping(item, f"{label} rule") for item in rules]


def _required_int(item: Mapping[str, Any], key: str, label: str) -> int:
    value = _optional_int(item.get(key))
    if value is None:
        raise ValueError(f"{label} rule has no {key}")
    return value


def _require_mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be a JSON object")
    return value


def _optional_mapping(value: Any) -> Mapping[str, Any] | None:
    return dict(value) if isinstance(value, Mapping) else None


def _optional_active_mapping(value: Any) -> Mapping[str, Any] | None:
    data = _optional_mapping(value)
    if not data:
        return None
    for key in ("enable", "enabled", "radiusEnable", "authEnable", "accountingEnable"):
        if key in data and not _optional_bool(data.get(key)):
            return None
    return data


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    return int(value)


def _optional_vlan_id(value: Any) -> int | None:
    vlan_id = _optional_int(value)
    if vlan_id in {None, 0}:
        return None
    return validate_vlan_id(vlan_id)


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _required_str(value: Any, label: str) -> str:
    if value is None or str(value).strip() == "":
        raise ValueError(f"{label} is required")
    return str(value)


def _optional_bool(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "enable", "enabled"}
    return bool(value)


def _optional_enabled(value: Any) -> bool | None:
    if value is None:
        return None
    return _enabled_string(value)


def _enabled_string(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"on", "enable", "enabled", "true", "1", "yes"}
    return bool(value)
