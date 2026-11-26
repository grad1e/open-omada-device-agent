"""Environment-backed runtime configuration for Open Omada Device Agent."""
from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _int(name: str, default: int) -> int:
    return int(os.getenv(name, str(default)), 0)


def _float(name: str, default: float) -> float:
    return float(os.getenv(name, str(default)))


def _bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


# Controller transport
CONTROLLER_HOST = os.getenv("OMADA_CONTROLLER_HOST", "").strip()
DISCOVERY_PORT = _int("OMADA_DISCOVERY_PORT", 29810)
MANAGE_PORT = _int("OMADA_MANAGE_PORT", 29814)
LOCAL_DISCOVERY_PORT = _int("OMADA_LOCAL_DISCOVERY_PORT", 0)
DISCOVERY_INTERVAL = _float("OMADA_DISCOVERY_INTERVAL", 5.0)
TCP_TIMEOUT = _float("OMADA_TCP_TIMEOUT", 15.0)
INFORM_INTERVAL = _float("OMADA_INFORM_INTERVAL", 3.0)
RECONNECT_DELAY = _float("OMADA_RECONNECT_DELAY", 3.0)
MANAGED_RECONNECT_ATTEMPTS = _int("OMADA_MANAGED_RECONNECT_ATTEMPTS", 3)

# TLS. Omada controllers commonly use a private/self-signed certificate on the
# device management channel, so certificate verification remains opt-in.
TLS_VERIFY = _bool("OMADA_TLS_VERIFY", False)
TLS_CA_FILE = os.getenv("OMADA_TLS_CA_FILE", "").strip()

PLACEHOLDER_MAC = "02:00:00:00:00:01"
DEVICE_MAC_FILE = Path(os.getenv("OMADA_DEVICE_MAC_FILE", ".omada-device-mac"))
RANDOM_MAC = "1"  # /sys/class/net/<if>/addr_assign_type: NET_ADDR_RANDOM


def _valid_mac(value: str) -> str | None:
    """``value`` as ``aa:bb:cc:dd:ee:ff``, or None. Accepts ``:``, ``-`` or no separator."""
    raw = value.strip().lower()
    parts = raw.replace("-", ":").split(":") if (":" in raw or "-" in raw) else [
        raw[i : i + 2] for i in range(0, len(raw), 2)
    ]
    if len(parts) != 6 or any(len(p) != 2 for p in parts):
        return None
    try:
        bytes.fromhex("".join(parts))
    except ValueError:
        return None
    mac = ":".join(parts)
    return None if mac == "00:00:00:00:00:00" else mac


def interface_mac(interface: str, sys_class_net: Path = Path("/sys/class/net")) -> str | None:
    """MAC address of ``interface``, or None when it has none we can use."""
    try:
        return _valid_mac((sys_class_net / interface / "address").read_text())
    except OSError:
        return None


def mac_is_random(interface: str, sys_class_net: Path = Path("/sys/class/net")) -> bool:
    """True when the kernel generated ``interface``'s MAC (it changes every boot).

    A bridge copies its MAC from a port, so the ports are checked too.
    """
    names = [interface]
    try:
        names += [p.name for p in (sys_class_net / interface / "brif").iterdir()]
    except OSError:
        pass
    mac = interface_mac(interface, sys_class_net)
    for name in names:
        try:
            kind = (sys_class_net / name / "addr_assign_type").read_text().strip()
        except OSError:
            continue
        if kind == RANDOM_MAC and interface_mac(name, sys_class_net) == mac:
            return True
    return False


def random_mac_message(bridge: str, device_mac: str) -> str:
    """Startup warning for a bridge whose MAC changes at every boot."""
    return (
        f"{bridge} has a random MAC that changes at every boot, so a controller "
        "\"Use Fixed IP Address\" reservation cannot match this AP. Pin a fixed "
        f"MAC with option macaddr '<MAC>' in the {bridge} device section of "
        "/etc/config/network, preferably the MAC on the device's label. To keep "
        f"the identity the controller knows, use {device_mac}; for any other MAC, "
        f"delete {DEVICE_MAC_FILE}, then forget and re-adopt the AP after the reboot."
    )


def default_device_mac(
    env: dict[str, str] | None = None,
    sys_class_net: Path = Path("/sys/class/net"),
    mac_file: Path | None = None,
    wait_seconds: float | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    """OMADA_DEVICE_MAC, else the remembered MAC, else the LAN bridge MAC.

    The bridge MAC is the one the AP's DHCP client uses, so a controller
    "Use Fixed IP Address" reservation for the AP matches its DHCP requests.
    The first MAC picked is saved to ``mac_file`` so the AP keeps its identity
    even on hardware whose MAC changes at every boot. At boot the bridge may
    not exist yet, so on OpenWrt it is awaited for up to ``wait_seconds``
    (default 10).
    """
    env = os.environ if env is None else env
    configured = env.get("OMADA_DEVICE_MAC", "").strip()
    if configured:
        mac = _valid_mac(configured)
        if mac is None:
            raise ValueError(
                f"OMADA_DEVICE_MAC={configured!r} is not a MAC address; "
                "use the form 02:aa:bb:cc:dd:ee"
            )
        return mac
    mac_file = DEVICE_MAC_FILE if mac_file is None else mac_file
    try:
        remembered = _valid_mac(mac_file.read_text())
    except OSError:
        remembered = None
    if remembered:
        return remembered
    bridge = env.get("OMADA_OPENWRT_LAN_BRIDGE", "br-lan").strip() or "br-lan"
    mac = interface_mac(bridge, sys_class_net)
    if wait_seconds is None:
        wait_seconds = 10.0 if Path("/etc/openwrt_release").exists() else 0.0
    waited = 0.0
    while mac is None and waited < wait_seconds:
        sleep(1.0)
        waited += 1.0
        mac = interface_mac(bridge, sys_class_net)
    if mac is None:
        logging.getLogger(__name__).warning(
            "No MAC address on %s; using the placeholder %s, which every such AP "
            "shares. Set OMADA_DEVICE_MAC to the MAC on the AP's label.",
            bridge,
            PLACEHOLDER_MAC,
        )
        return PLACEHOLDER_MAC
    try:
        mac_file.write_text(mac + "\n")
    except OSError:
        pass
    return mac


# Reference device identity. The current implementation has been exercised with
# an EAP110 v4-compatible profile, but every field remains configurable.
MAC = default_device_mac()
DEVICE_NAME = os.getenv("OMADA_DEVICE_NAME", "OpenOmada-AP")
MODEL = os.getenv("OMADA_DEVICE_MODEL", "EAP110")
MODEL_VERSION = os.getenv("OMADA_DEVICE_MODEL_VERSION", "4.0")
HARDWARE_VERSION = os.getenv("OMADA_DEVICE_HARDWARE_VERSION", "4.0")
FIRMWARE_VERSION = os.getenv("OMADA_DEVICE_FIRMWARE_VERSION", "5.0.4")
CUSTOMIZE_REGION = _int("OMADA_CUSTOMIZE_REGION", 841)

# Advertised device IP. "local" (default) advertises the address this host uses
# to reach the controller. Set a concrete address to override it, or "auto" for
# a best-effort public-IP lookup through a third-party service.
DEVICE_IP = os.getenv("OMADA_DEVICE_IP", "local").strip()
PUBLIC_IP_LOOKUP_URL = os.getenv("OMADA_PUBLIC_IP_LOOKUP_URL", "https://api.ipify.org").strip()
PUBLIC_IP_LOOKUP_TIMEOUT = _float("OMADA_PUBLIC_IP_LOOKUP_TIMEOUT", 2.0)

# Minimal wired-uplink report used by managed informs.
LAN_RATE = _float("OMADA_LAN_RATE", 100.0)
LAN_DUPLEX = _int("OMADA_LAN_DUPLEX", 1)
LAN_PORT = os.getenv("OMADA_LAN_PORT", "LAN")

# Client tracking. In AP mode this is an observation source, not an assertion
# that the AP is the DHCP server. If the file does not exist, informs simply
# omit client entries.
DHCP_LEASE_FILE = os.getenv("OMADA_DHCP_LEASE_FILE", "/tmp/dhcp.leases").strip()

# Passive client address/name learning on the AP's bridges (DHCP, ARP, IPv6
# neighbour discovery, mDNS). "auto" listens on every bridge, "off" disables
# it, or give a comma-separated interface list.
CLIENT_SNOOP_INTERFACES = os.getenv("OMADA_CLIENT_SNOOP_INTERFACES", "auto").strip()

# Optional Linux/OpenWrt sysfs LED control. Leave unset unless the operator has
# mapped a real AP status LED brightness file.
LED_BRIGHTNESS_PATH = os.getenv("OMADA_LED_BRIGHTNESS_PATH", "").strip()
LED_ON_VALUE = os.getenv("OMADA_LED_ON_VALUE", "1")
LED_OFF_VALUE = os.getenv("OMADA_LED_OFF_VALUE", "0")
LED_TRIGGER_PATH = os.getenv("OMADA_LED_TRIGGER_PATH", "").strip()
LED_LOCATE_TRIGGER = os.getenv("OMADA_LED_LOCATE_TRIGGER", "timer")
LED_DEFAULT_TRIGGER = os.getenv("OMADA_LED_DEFAULT_TRIGGER", "none")

# Optional client operations. Reconnect/deauth uses OpenWrt hostapd ubus
# objects, while block/unblock uses an nftables bridge table to drop traffic
# from the client MAC after association.
HOSTAPD_UBUS_IFACE = os.getenv("OMADA_HOSTAPD_UBUS_IFACE", "").strip()
CLIENT_BLOCK_INTERFACE = os.getenv("OMADA_CLIENT_BLOCK_INTERFACE", "").strip()
CLIENT_RATE_LIMIT_INTERFACE = os.getenv("OMADA_CLIENT_RATE_LIMIT_INTERFACE", "").strip()

# Optional captive-portal fallback. On OpenWrt, openNDS is preferred when
# installed. These settings are used only by the older nftables-only redirect
# path and do not render the Omada Controller portal.
PORTAL_INTERFACE = os.getenv("OMADA_PORTAL_INTERFACE", "").strip()
PORTAL_REDIRECT_PORT = _int("OMADA_PORTAL_REDIRECT_PORT", 8080)

# Optional management VLAN reconciliation. These must be explicit because the
# wrong OpenWrt network target can move the AP out of reach.
MANAGEMENT_VLAN_INTERFACE = os.getenv("OMADA_MANAGEMENT_VLAN_INTERFACE", "").strip()
MANAGEMENT_VLAN_DEVICE = os.getenv("OMADA_MANAGEMENT_VLAN_DEVICE", "").strip()

# OpenWrt bootstrap. These settings are applied idempotently before ECSP
# discovery so a Wi-Fi-only OpenWrt device can create br-lan, bind openNDS, and
# optionally expose lab management on the WAN uplink.
OPENWRT_BOOTSTRAP = _bool("OMADA_OPENWRT_BOOTSTRAP", True)
OPENWRT_BOOTSTRAP_LAN = _bool("OMADA_OPENWRT_BOOTSTRAP_LAN", True)
OPENWRT_LAN_INTERFACE = os.getenv("OMADA_OPENWRT_LAN_INTERFACE", "lan").strip()
OPENWRT_LAN_BRIDGE = os.getenv("OMADA_OPENWRT_LAN_BRIDGE", "br-lan").strip()
OPENWRT_LAN_IPADDR = os.getenv("OMADA_OPENWRT_LAN_IPADDR", "192.168.1.1/24").strip()
OPENWRT_BOOTSTRAP_OPENNDS = _bool("OMADA_OPENWRT_BOOTSTRAP_OPENNDS", True)
OPENNDS_GATEWAY_PORT = _int("OMADA_OPENNDS_GATEWAY_PORT", 2050)
# Bridge openNDS guards. Empty uses OMADA_OPENWRT_LAN_BRIDGE. On an AP whose
# uplink router is on the LAN bridge, use a separate routed guest bridge.
OPENNDS_INTERFACE = os.getenv("OMADA_OPENNDS_INTERFACE", "").strip()
# UCI network that portal SSIDs join (e.g. "guest"). Empty keeps them on "lan".
PORTAL_NETWORK = os.getenv("OMADA_PORTAL_NETWORK", "").strip()
OPENNDS_GATEWAY_NAME = os.getenv("OMADA_OPENNDS_GATEWAY_NAME", "").strip()
OPENWRT_ENABLE_WAN_MANAGEMENT = _bool("OMADA_OPENWRT_ENABLE_WAN_MANAGEMENT", False)
OPENWRT_WAN_ZONE = os.getenv("OMADA_OPENWRT_WAN_ZONE", "wan").strip()
# ISO 3166-1 alpha-2 country applied to every wifi-device (e.g. IN, US, DE).
# The controller never sends the site country to the AP.
WIFI_COUNTRY = os.getenv("OMADA_WIFI_COUNTRY", "").strip().upper()

# Lab/research compatibility. When enabled, known controller-side/default
# configuration families that currently have no local OpenWrt side effect are
# acknowledged instead of failing the entire SET_REQUEST batch. Keep disabled
# outside controlled protocol labs.
LAB_ACK_CONTROL_PLANE_CONFIG = _bool("OMADA_LAB_ACK_CONTROL_PLANE_CONFIG", False)

# ECSP V2 framing/protocol identity.
ECSP_VERSION = os.getenv("OMADA_ECSP_VERSION", "2.3.0")
ECSP_VER_CAP = _int("OMADA_ECSP_VER_CAP", 2)

# Controller/site routing. The same Site ID is used for site-scoped discovery
# and, when available, for Omada External Portal redirects. The human-readable
# site name is kept as a fallback for controllers/configs that do not expose an
# ID in the local environment.
CONTROLLER_ID = os.getenv("OMADA_CONTROLLER_ID", "").strip()
DEST_OMADAC_ID = os.getenv("OMADA_DEST_OMADAC_ID", "").strip()
SITE_ID = os.getenv("OMADA_SITE_ID", "").strip()
SITE_NAME = os.getenv("OMADA_SITE_NAME", "").strip()

# Legacy V2 Device Account authentication. No password default is provided and
# credentials are never persisted to managed state.
DEVICE_USERNAME = os.getenv("OMADA_DEVICE_USERNAME", "").strip()
DEVICE_PASSWORD = os.getenv("OMADA_DEVICE_PASSWORD", "")
DEVICE_CIPHER_TYPE = _int("OMADA_DEVICE_CIPHER_TYPE", 5)

# Keep the historical filename for backward compatibility with existing labs.
_state_mac = MAC.replace(":", "").replace("-", "").lower()
STATE_FILE = os.getenv("OMADA_STATE_FILE", f".omada-agent-state-{_state_mac}.json")


def validate_runtime_config() -> None:
    """Validate fields required before network I/O begins."""
    if not CONTROLLER_HOST:
        raise RuntimeError(
            "OMADA_CONTROLLER_HOST is required. Copy .env.example to .env and configure your controller."
        )
    if not (1 <= DISCOVERY_PORT <= 65535 and 1 <= MANAGE_PORT <= 65535):
        raise RuntimeError("OMADA discovery/manage ports must be between 1 and 65535")
    if TLS_CA_FILE and not Path(TLS_CA_FILE).expanduser().exists():
        raise RuntimeError(f"OMADA_TLS_CA_FILE does not exist: {TLS_CA_FILE}")
