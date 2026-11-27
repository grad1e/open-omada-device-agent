"""Host/platform capability detection for Omada AP features.

The ECSP component map says what the protocol handler can parse.  This module
answers a different question: what the local host can actually enforce.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import replace

from ....application.contracts import PlatformCapabilities
from ....contexts.wireless.domain import RadioBand
from .iw_phy import DFS_REGIONS, parse_dfs_region, parse_iw_list


CommandExists = Callable[[str], str | None]
CommandOutput = Callable[[tuple[str, ...]], str | None]
CommandSucceeds = Callable[[tuple[str, ...]], bool]


def detect_platform_capabilities(
    *,
    env: Mapping[str, str] | None = None,
    command_exists: CommandExists | None = None,
    command_output: CommandOutput | None = None,
    command_succeeds: CommandSucceeds | None = None,
) -> PlatformCapabilities:
    values = env if env is not None else os.environ
    which = command_exists or shutil.which
    succeeds = command_succeeds or _run_command_succeeds

    platform = _platform(values)
    has_uci = which("uci") is not None
    has_ubus = which("ubus") is not None
    has_nft = which("nft") is not None
    has_hostapd = which("hostapd") is not None
    has_dnsmasq = which("dnsmasq") is not None
    has_opennds = which("opennds") is not None and which("ndsctl") is not None
    openwrt = platform == "openwrt" or (platform == "auto" and has_uci and has_ubus)

    radio_bands = _radio_bands(values.get("OMADA_RADIO_BANDS", "2g"))
    max_ssids = _int(values, "OMADA_MAX_SSIDS", 4)
    wlan_possible = openwrt and has_uci
    # ``hostapd -v<feature>`` exits 0 when the build has it (wpad-basic has
    # SAE and 802.11r; Validated on wpad-basic-mbedtls 2025.08.26).
    hostapd_sae = wlan_possible and has_hostapd and succeeds(("hostapd", "-vsae"))
    hostapd_11r = wlan_possible and has_hostapd and succeeds(("hostapd", "-v11r"))
    # hostapd has no -v feature name for 802.11k/v. RRM (802.11k) is always
    # built in (Validated: wpad-basic-mbedtls accepts ieee80211k). 802.11v
    # needs CONFIG_WNM_AP, which wpad-basic lacks: hostapd then refuses
    # bss_transition and the radio stays down (Observed). Its config key
    # wnm_sleep_mode is only in WNM_AP builds (Inferred: absent from
    # wpad-basic-mbedtls 2025.08.26).
    hostapd_11k = wlan_possible and has_hostapd
    hostapd_path = which("hostapd") if hostapd_11k else None
    hostapd_11v = bool(hostapd_path) and succeeds(("grep", "-q", "wnm_sleep_mode", str(hostapd_path)))
    iw_output = _iw_list_output(
        openwrt=openwrt,
        command_exists=which,
        command_output=command_output or _run_command_output,
    )
    max_ssids = _cap_max_ssids_by_iw(max_ssids=max_ssids, iw_output=iw_output)
    radio_hardware = tuple(
        hw for hw in parse_iw_list(iw_output or "") if hw.band in radio_bands
    )
    if radio_hardware:
        reg_output = _iw_output(
            ("reg", "get"),
            openwrt=openwrt,
            command_exists=which,
            command_output=command_output or _run_command_output,
        )
        if parse_dfs_region(reg_output or "") in DFS_REGIONS:
            radio_hardware = tuple(replace(hw, dfs=True) for hw in radio_hardware)

    return PlatformCapabilities(
        platform="openwrt" if openwrt else platform,
        has_uci=has_uci,
        has_ubus=has_ubus,
        has_nft=has_nft,
        has_hostapd=has_hostapd,
        has_dnsmasq=has_dnsmasq,
        has_opennds=has_opennds,
        radio_bands=radio_bands,
        max_ssids=max(0, max_ssids),
        supports_wlan_config=_feature(values, "OMADA_CAP_WLAN", wlan_possible),
        supports_wpa2_psk=_feature(values, "OMADA_CAP_WPA2_PSK", wlan_possible),
        supports_wpa3_psk=_feature(values, "OMADA_CAP_WPA3_PSK", hostapd_sae),
        supports_80211r=_feature(values, "OMADA_CAP_80211R", hostapd_11r),
        supports_80211k=_feature(values, "OMADA_CAP_80211K", hostapd_11k),
        supports_80211v=_feature(values, "OMADA_CAP_80211V", hostapd_11v),
        supports_wpa_enterprise=_feature(values, "OMADA_CAP_WPA_ENTERPRISE", False),
        supports_radius=_feature(values, "OMADA_CAP_RADIUS", False),
        supports_ssid_vlan=_feature(values, "OMADA_CAP_SSID_VLAN", False),
        supports_dynamic_vlan=_feature(values, "OMADA_CAP_DYNAMIC_VLAN", False),
        supports_management_vlan=_feature(values, "OMADA_CAP_MANAGEMENT_VLAN", False),
        supports_portal=_feature(values, "OMADA_CAP_PORTAL", openwrt and has_opennds),
        supports_dhcp_tracking=_feature(values, "OMADA_CAP_DHCP_TRACKING", openwrt and has_ubus),
        supports_option82=_feature(values, "OMADA_CAP_OPTION82", False),
        supports_led_control=_feature(
            values,
            "OMADA_CAP_LED",
            bool(
                values.get("OMADA_LED_BRIGHTNESS_PATH", "").strip()
                or values.get("OMADA_LED_TRIGGER_PATH", "").strip()
            ),
        ),
        supports_client_operations=_feature(
            values,
            "OMADA_CAP_CLIENT_OPERATIONS",
            openwrt and has_ubus,
        ),
        supports_client_rate_limits=_feature(values, "OMADA_CAP_CLIENT_RATE_LIMITS", False),
        # ``iw dev <ap> scan ap-force`` works when iw reports the radios.
        supports_rogue_ap_scan=_feature(
            values,
            "OMADA_CAP_ROGUE_AP_SCAN",
            openwrt and bool(radio_hardware),
        ),
        # Tools -> Network Check runs BusyBox ping/traceroute/nslookup.
        supports_network_check=_feature(values, "OMADA_CAP_NETWORK_CHECK", openwrt),
        # Config -> Services -> SNMP writes /etc/config/snmpd for net-snmp.
        supports_snmp=_feature(
            values,
            "OMADA_CAP_SNMP",
            openwrt and has_uci and which("snmpd") is not None,
        ),
        # Config -> Services -> LLDP runs lldpd on the LAN ports and reports
        # the neighbours it hears.
        supports_lldp=_feature(
            values,
            "OMADA_CAP_LLDP",
            openwrt and has_uci and which("lldpd") is not None and which("lldpcli") is not None,
        ),
        # The site time zone from the controller sets OpenWrt's system time zone.
        supports_time_zone=_feature(values, "OMADA_CAP_TIMEZONE", openwrt and has_uci),
        # Device Config -> IP Settings rewrites the LAN address, so it is
        # opt-in; a wrong push could cut the AP off from the controller.
        supports_ip_settings=_feature(values, "OMADA_CAP_IP_SETTINGS", False),
        radio_hardware=radio_hardware,
    )


def capability_summary(capabilities: PlatformCapabilities) -> str:
    bands = ",".join(band.value for band in capabilities.radio_bands) or "none"
    enabled = []
    for field in (
        "supports_wlan_config",
        "supports_wpa2_psk",
        "supports_wpa3_psk",
        "supports_80211r",
        "supports_80211k",
        "supports_80211v",
        "supports_wpa_enterprise",
        "supports_radius",
        "supports_ssid_vlan",
        "supports_dynamic_vlan",
        "supports_management_vlan",
        "supports_portal",
        "supports_dhcp_tracking",
        "supports_option82",
        "supports_led_control",
        "supports_client_operations",
        "supports_client_rate_limits",
        "supports_rogue_ap_scan",
        "supports_network_check",
        "supports_snmp",
        "supports_lldp",
        "supports_time_zone",
        "supports_ip_settings",
    ):
        if getattr(capabilities, field):
            enabled.append(field.removeprefix("supports_"))
    features = ",".join(enabled) if enabled else "none"
    return (
        f"platform={capabilities.platform} bands={bands} maxSsids={capabilities.max_ssids} "
        f"tools=uci:{int(capabilities.has_uci)},ubus:{int(capabilities.has_ubus)},"
        f"nft:{int(capabilities.has_nft)},hostapd:{int(capabilities.has_hostapd)},"
        f"dnsmasq:{int(capabilities.has_dnsmasq)},opennds:{int(capabilities.has_opennds)} "
        f"features={features}"
    )


def _platform(env: Mapping[str, str]) -> str:
    value = env.get("OMADA_PLATFORM", "auto").strip().lower()
    if value not in {"auto", "openwrt", "generic"}:
        raise ValueError("OMADA_PLATFORM must be auto, openwrt or generic")
    return value


def _radio_bands(raw: str) -> tuple[RadioBand, ...]:
    bands = []
    for item in raw.split(","):
        value = item.strip().lower()
        if not value:
            continue
        try:
            bands.append(RadioBand(value))
        except ValueError as exc:
            raise ValueError(f"unsupported OMADA_RADIO_BANDS entry: {value}") from exc
    return tuple(dict.fromkeys(bands))


def _feature(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = env.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "enable", "enabled"}


def _int(env: Mapping[str, str], name: str, default: int) -> int:
    raw = env.get(name)
    if raw is None or raw.strip() == "":
        return default
    return int(raw, 0)


def _iw_list_output(
    *,
    openwrt: bool,
    command_exists: CommandExists,
    command_output: CommandOutput,
) -> str | None:
    return _iw_output(
        ("list",),
        openwrt=openwrt,
        command_exists=command_exists,
        command_output=command_output,
    )


def _iw_output(
    args: tuple[str, ...],
    *,
    openwrt: bool,
    command_exists: CommandExists,
    command_output: CommandOutput,
) -> str | None:
    if not openwrt:
        return None
    iw = command_exists("iw")
    if iw is None:
        return None
    return command_output((iw, *args))


def _cap_max_ssids_by_iw(*, max_ssids: int, iw_output: str | None) -> int:
    configured = max(0, max_ssids)
    if configured <= 1 or iw_output is None:
        return configured

    detected = _iw_ap_interface_limit(iw_output)
    if detected is None:
        return configured
    return min(configured, max(0, detected))


def _iw_ap_interface_limit(output: str) -> int | None:
    if "interface combinations are not supported" in output:
        return 1 if _iw_supports_ap_mode(output) else None

    limits = []
    for modes, limit in re.findall(r"#\{\s*([^}]*)\s*\}\s*<=\s*(\d+)", output):
        if any(mode.strip() == "AP" for mode in modes.split(",")):
            limits.append(int(limit))
    if not limits:
        return None
    return max(limits)


def _iw_supports_ap_mode(output: str) -> bool:
    for line in output.splitlines():
        if line.strip() == "* AP":
            return True
    return False


def _run_command_succeeds(args: tuple[str, ...]) -> bool:
    try:
        result = subprocess.run(args, capture_output=True, check=False, timeout=2)
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def _run_command_output(args: tuple[str, ...]) -> str | None:
    try:
        result = subprocess.run(
            args,
            capture_output=True,
            check=False,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout
