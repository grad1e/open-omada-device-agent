"""Conservative ECSP V2 capabilities advertised by the fake EAP.

The controller intersects this map with its own component table during
DEVICE_NEGOTIATION.  Advertising an empty ``components_v2`` map causes the
controller to build an invalid ComponentInfo and mark the AP incompatible.

Keep this baseline intentionally small: every version below was observed in
Controller 6.2.14.11's SYSTEM_NEGOTIATION response.  More AP capabilities can
be added incrementally as their SET/GET/INFORM behavior is implemented.
"""
from __future__ import annotations

from .application.contracts import PlatformCapabilities


AP_COMPONENTS_V2: dict[str, str] = {
    # Core managed-device lifecycle/config synchronization.
    "system": "2.0",
    "configVersion": "1.0",
    "time": "1.0",
    "informInterval": "1.2",
    "devInform": "2.0",
}

AP_WLAN_COMPONENTS_V2: dict[str, str] = {
    # Controller 6.2.14.11 component table:
    # wlanBasic=2.2, ssid=2.20, wlanInform=2.1, ssidInform=2.0.
    "wlanBasic": "2.2",
    "ssid": "2.20",
    "wlanInform": "2.1",
    "ssidInform": "2.0",
}


def ap_components_v2(
    capabilities: PlatformCapabilities | None = None,
) -> dict[str, str]:
    """Return a copy so callers cannot mutate the module-level baseline."""
    components = dict(AP_COMPONENTS_V2)
    if capabilities is None:
        return components
    if capabilities.supports_wlan_config:
        components.update(AP_WLAN_COMPONENTS_V2)
    elif capabilities.has_ubus:
        components.update(
            {
                "wlanInform": AP_WLAN_COMPONENTS_V2["wlanInform"],
                "ssidInform": AP_WLAN_COMPONENTS_V2["ssidInform"],
            }
        )
    if capabilities.supports_dhcp_tracking or capabilities.has_ubus:
        components["clientInform"] = "2.0"
    if capabilities.supports_led_control:
        components["led"] = "1.0"
    if capabilities.supports_management_vlan:
        components["mVlan"] = "1.0"
    if capabilities.supports_client_rate_limits:
        components["clientRateLimit"] = "1.0"
    if capabilities.supports_wlan_config:
        # EAP650 versions in Controller 6.3.0.111: per-radio Maximum Associated
        # Clients (lb), RSSI Threshold (rssiFilter), U-APSD (qos), OFDMA (wlanAdv).
        components.update(
            {"lb": "1.0", "rssiFilter": "1.0", "qos": "1.0", "wlanAdv": "1.1"}
        )
        # WLAN Schedule: without a scheduler component the controller marks
        # the schedule "not supported" on Configuration Result, although it
        # still sends it and the UCI adapter applies it (Observed: lab,
        # Controller 6.3.0.111, version 1.0 clears it).
        components["scheduler"] = "1.0"
    if capabilities.supports_rogue_ap_scan:
        # Controller 6.3.0.111 model templates list rogueAp=3.0 and
        # rogueApInform=2.0 for the EAP650; results travel in ``rogueApList``.
        components.update({"rogueAp": "3.0", "rogueApInform": "2.0"})
    if capabilities.supports_network_check:
        # Controller 6.3.0.111 shows Tools -> Network Check when the AP lists
        # dnslookup/arptable >= 1.0; each tool is its own SET key.
        components.update(
            {"ping": "1.0", "traceroute": "1.0", "dnslookup": "1.0", "arptable": "1.0"}
        )
    if capabilities.supports_snmp:
        # Controller 6.3.0.111 SET_SNMP component; the SET key is ``snmp``.
        # ``service`` >= 1.1 (ApImage.supportSnmpV3Plus) makes the controller
        # keep the SNMPv3 security level, auth/privacy modes and privacy
        # password; below that it sends only the v3 username and password.
        components.update({"snmp": "1.0", "service": "1.1"})
    if capabilities.supports_lldp:
        # Controller 6.3.0.111 shows the per-AP LLDP switch for devCap.supportLldp
        # and treats enable=1 as unsupported without the ``lldp`` component;
        # neighbours travel in the inform key ``lldp`` (``lldpInform``).
        components.update({"lldp": "1.0", "lldpInform": "1.0"})
    return components
