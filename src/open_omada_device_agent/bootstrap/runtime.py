"""The sole default wiring point for application ports and concrete adapters."""
from dataclasses import dataclass
from functools import lru_cache, partial
import logging
import threading

from ..application.configuration import ApplyDeviceConfiguration
from ..application.contracts import PlatformCapabilities
from ..adapters.outbound.openwrt.model_select import resolve_model
from ..adapters.outbound.openwrt.bootstrap import OpenWrtBootstrapConfig, OpenWrtStartupBootstrap
from ..adapters.outbound.openwrt.device_commands import OpenWrtClientControlAdapter, SysfsLedAdapter
from ..adapters.outbound.openwrt.uci import OpenWrtUciAdapter
from ..adapters.outbound.openwrt.capabilities import detect_platform_capabilities
from ..adapters.outbound.openwrt.portal_runtime import OpenWrtPortalRuntime
from ..adapters.outbound.openwrt.rogue_ap_scan import OpenWrtRogueApScanner
from ..adapters.outbound.openwrt.network_check import OpenWrtNetworkCheck
from ..adapters.outbound.openwrt.snmp import OpenWrtSnmpAdapter
from ..adapters.outbound.openwrt.lldp import OpenWrtLldpAdapter
from ..adapters.outbound.openwrt.timezone import OpenWrtTimeZoneAdapter
from ..adapters.outbound.openwrt.dhcp_fallback import DhcpFallback, start_dhcp_fallback
from ..adapters.outbound.openwrt.ip_settings import OpenWrtIpSettings
from ..application.notifications import OutboundNotifications
from ..adapters.outbound.openwrt.client_snoop import BridgeClientSnooper
from ..adapters.outbound.openwrt.client_observations import (
    client_stats_payload,
    clients_from_dhcp_leases,
    load_dnsmasq_leases,
    merge_associated_wireless_client_states,
)
from ..network_tools import forget_local_ip, get_device_ip
from ..config import mac_is_random, random_mac_message
from ..adapters.outbound.openwrt.device_profile import GenericOpenWrtAccessPointProfile
from .settings import AgentSettings
from ..projections.inform import InformAssembler, LanObservation
from ..contexts.lifecycle.infrastructure.session_state import JsonSessionStateRepository
from ..adapters.outbound.openwrt.telemetry import (
    OpenWrtWirelessTelemetry,
    collect_openwrt_wireless_clients,
    collect_openwrt_wireless_inform,
)
from ..adapters.outbound.openwrt.opennds import collect_opennds_clients


log = logging.getLogger("open_omada.bootstrap")


@dataclass(frozen=True)
class AgentRuntime:
    configuration: ApplyDeviceConfiguration
    inform: InformAssembler
    state_repository: JsonSessionStateRepository
    settings: AgentSettings
    device_profile: GenericOpenWrtAccessPointProfile
    capabilities: PlatformCapabilities
    notifications: OutboundNotifications
    # Set when the LAN address changes, so the session reconnects from it.
    session_reset: threading.Event


@lru_cache(maxsize=1)
def build_runtime(settings: AgentSettings) -> AgentRuntime:
    """Build production dependencies; tests may construct the use cases directly."""
    capabilities = detect_platform_capabilities(env=dict(settings.capability_environment))
    if mac_is_random(settings.openwrt_lan_bridge):
        log.warning("%s", random_mac_message(settings.openwrt_lan_bridge, settings.mac))
    def controller_host() -> str | None:
        # Saved state is only used for this same host, so the setting is enough.
        return settings.controller_host or None

    startup_bootstrap = OpenWrtStartupBootstrap(
        controller_host=controller_host,
        config=OpenWrtBootstrapConfig(
            enabled=settings.openwrt_bootstrap,
            ensure_lan=settings.openwrt_bootstrap_lan,
            lan_interface=settings.openwrt_lan_interface,
            lan_bridge=settings.openwrt_lan_bridge,
            lan_ipaddr=settings.openwrt_lan_ipaddr,
            ensure_opennds=settings.openwrt_bootstrap_opennds,
            opennds_gateway_port=settings.opennds_gateway_port,
            opennds_gateway_name=settings.opennds_gateway_name,
            enable_wan_management=settings.openwrt_enable_wan_management,
            wan_zone=settings.openwrt_wan_zone,
            wifi_country=settings.wifi_country,
            opennds_interface=settings.opennds_interface,
        )
    )
    bootstrap_result = startup_bootstrap.apply(capabilities)
    if bootstrap_result.wireless_changed:
        # The regulatory domain changed, so the iw channel list is stale.
        capabilities = detect_platform_capabilities(env=dict(settings.capability_environment))
    prune_result = startup_bootstrap.prune_auto_channels(capabilities)
    if prune_result.warnings:
        log.warning("Auto channel cleanup warning(s): %s", "; ".join(prune_result.warnings))
    if bootstrap_result.warnings:
        log.warning(
            "OpenWrt bootstrap completed with warning(s): %s",
            "; ".join(bootstrap_result.warnings),
        )
    wireless_telemetry = OpenWrtWirelessTelemetry()
    rogue_ap_scanner = OpenWrtRogueApScanner()
    client_snooper = BridgeClientSnooper(settings.client_snoop_interfaces)
    notifications = OutboundNotifications()
    state_repository = JsonSessionStateRepository(
        settings.state_file,
        device_mac=settings.mac,
        controller_host=settings.controller_host,
    )

    def controller_endpoint() -> tuple[str, int] | None:
        state = state_repository.load()
        return (state.controller_host, state.manage_port) if state else None

    session_reset = threading.Event()

    def address_changed() -> None:
        # Report the new address in the next session, not a cached old one.
        forget_local_ip()
        session_reset.set()

    ip_settings = OpenWrtIpSettings(
        interface=settings.openwrt_lan_interface,
        controller_endpoint=controller_endpoint,
        on_address_changed=address_changed,
    )
    if capabilities.supports_ip_settings:
        start_dhcp_fallback(
            DhcpFallback(interface=settings.openwrt_lan_interface, on_change=address_changed)
        )
    settings = resolve_model(settings, capabilities)
    profile = GenericOpenWrtAccessPointProfile(
        settings,
        ip_address=partial(
            get_device_ip,
            settings.device_ip,
            settings.controller_host,
            settings.public_ip_lookup_url,
            settings.public_ip_lookup_timeout,
        ),
        capabilities=capabilities,
        lan_setting=ip_settings.report if capabilities.supports_ip_settings else None,
    )
    lldp = OpenWrtLldpAdapter(bridge=settings.openwrt_lan_bridge, lan_port=settings.lan_port)
    return AgentRuntime(
        configuration=ApplyDeviceConfiguration(
            capability_detector=lambda: capabilities,
            platform_ports=(
                OpenWrtUciAdapter(
                    management_vlan_interface=settings.management_vlan_interface,
                    management_vlan_device=settings.management_vlan_device,
                    portal_network=settings.portal_network,
                    state_file=settings.wireless_state_file,
                ),
                OpenWrtPortalRuntime(
                    interface=settings.portal_interface,
                    redirect_port=settings.portal_redirect_port,
                    controller_host=controller_host,
                ),
            ),
            command_ports=(
                SysfsLedAdapter(
                    brightness_path=settings.led_brightness_path,
                    trigger_path=settings.led_trigger_path,
                    on_value=settings.led_on_value,
                    off_value=settings.led_off_value,
                    locate_trigger=settings.led_locate_trigger,
                    default_trigger=settings.led_default_trigger,
                ),
                OpenWrtClientControlAdapter(
                    hostapd_iface=settings.hostapd_ubus_iface,
                    block_interface=settings.client_block_interface,
                    rate_limit_interface=settings.client_rate_limit_interface,
                ),
                rogue_ap_scanner,
                OpenWrtNetworkCheck(notifications.put),
                OpenWrtSnmpAdapter(),
                lldp,
                OpenWrtTimeZoneAdapter(),
                ip_settings,
            ),
            allow_ack_only_config=settings.lab_ack_control_plane_config,
        ),
        inform=InformAssembler(
            device_info=profile.device_info,
            lan=LanObservation(settings.lan_rate, settings.lan_duplex, settings.lan_port),
            clients=lambda: merge_associated_wireless_client_states(
                collect_openwrt_wireless_clients(capabilities=capabilities),
                client_snooper.clients(),
                clients_from_dhcp_leases(load_dnsmasq_leases(settings.dhcp_lease_file)),
                collect_opennds_clients(capabilities=capabilities),
            ),
            client_projection=client_stats_payload,
            wireless_projection=lambda: collect_openwrt_wireless_inform(
                capabilities=capabilities,
                telemetry=wireless_telemetry,
            ),
            rogue_aps=rogue_ap_scanner.inform_items,
            lldp=lldp.inform_item if capabilities.supports_lldp else None,
        ),
        state_repository=state_repository,
        settings=settings,
        device_profile=profile,
        capabilities=capabilities,
        notifications=notifications,
        session_reset=session_reset,
    )
