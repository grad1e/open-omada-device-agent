# Captive portal on OpenWrt

On OpenWrt, captive portal enforcement should use `openNDS` instead of the
agent's older nftables-only fallback. OpenWrt 25 snapshots use `apk`:

```bash
apk update
apk add opennds dnsmasq-full conntrack
```

Older OpenWrt releases may still use `opkg`:

```bash
opkg update
opkg install opennds dnsmasq-full conntrack
```

`scripts/deploy-openwrt.sh` installs these for you, plus `snmpd`
(`snmpd-ssl` with apk) for Services → SNMP. snmpd stays stopped until SNMP is
turned on in the controller.

The agent auto-detects portal capability when `opennds` and `ndsctl` are
available. If `OMADA_CAP_PORTAL=false` is set in `.env`, that explicit override
disables portal acceptance.

At startup on OpenWrt, the agent also applies an idempotent platform bootstrap:
it sets `bridge_empty=1` on the configured LAN bridge so Wi-Fi-only devices can
create `br-lan`, reloads network/Wi-Fi when that prerequisite changed, enables
openNDS on `OMADA_OPENNDS_INTERFACE` (default: the LAN bridge) when installed, and disables openNDS preemptive
authentication so clients follow the Omada ThemeSpec redirect. It also sets
`gatewayfqdn=disable` so the first openNDS preauth redirect uses the gateway IP
instead of the default `status.client` name; this keeps macOS/iOS captive checks
working when a client has custom DNS such as `8.8.8.8`. Lab WAN access for
SSH/LuCI is available only through `OMADA_OPENWRT_ENABLE_WAN_MANAGEMENT=true`.

## Portal on an AP that is not the router

openNDS treats every device on its interface as a portal client, including
the upstream router. On an AP whose uplink is on the LAN bridge, openNDS on
that bridge cuts the AP off from the gateway and the controller, so the agent
refuses to start openNDS on the interface that holds the default route.

Put portal SSIDs on a routed guest network instead: a UCI network with its own
subnet, DHCP and NAT. Set `OMADA_PORTAL_NETWORK` to that network and
`OMADA_OPENNDS_INTERFACE` to its bridge. The agent attaches portal SSIDs to the
guest network and starts openNDS when the first portal SSID arrives. See
[Captive portal on OpenWrt](configuration.md#captive-portal-on-openwrt)
for a worked example.

Validated behavior today:

- openNDS captures unauthenticated WLAN clients on OpenWrt.
- Omada `portalFreePolicyConfig` is translated into openNDS walled-garden FQDN
  and preauthenticated IP rules.
- When Omada sends a portal URL through `portalConfigList` or a `/portal/...`
  URL in `portalFreePolicyConfig`, the agent writes an openNDS ThemeSpec that
  redirects the captive browser to that Omada-configured URL instead of the
  stock openNDS splash. For the controller's own login page (`/portal/entry`)
  the redirect appends the query names it reads: `cid`, `ap`, `ssid`, `rid`,
  `t`, `u` and `clientIp`. For an External Portal server it appends
  `clientMac`, `apMac`, `ssidName`, `t`, `radioId`, `site`, and `redirectUrl`,
  matching the TP-Link External Portal contract.
- The login page host (or its IP and port) is reachable before login, so the
  captive browser can load it.
- End to end with Controller 6.3.0.111's built-in portal: a phone on the
  portal SSID gets the login page, taps Log In, and openNDS authenticates it.
- The openNDS adapter disables `allow_preemptive_authentication` and relies on
  the classic HTTP redirect path; this avoids Android CPD/RFC8910 opening a
  generic status page instead of the Omada-configured portal URL.
- The openNDS adapter disables `gatewayfqdn`, so captive clients are redirected
  to the gateway IP and do not need to resolve the local-only `status.client`
  hostname.
- Omada `clientConfig.unauth` and controller logins (`EVENT_PORTAL_AUTH`) are
  translated into `ndsctl auth/deauth`. A login lasts as long as the portal's
  Authentication Timeout.
- When `conntrack` is installed, deauth also clears flows for the client's IP
  so previously authenticated sessions stop immediately.
- `ndsctl json` is merged into Omada client telemetry as portal state. Only
  logged-in clients are reported in `portalAuthClients`.
- Client MACs, AP MACs, ECSP header MACs, `deviceInfo.mainMac`, SSID BSSIDs,
  and portal redirect MAC parameters are serialized in Omada's controller-facing
  format: `AA-BB-CC-DD-EE-FF`.
- The old `inet openomada_portal` nftables fallback is skipped when openNDS is
  present.

Current limitation:

- The agent does not host the external portal UI and does not submit
  `/hotspot/extPortal/auth` itself. The external portal configured in Omada must
  preserve the redirect query parameters and perform Controller authorization.
- Guest logout (the controller's logout domain) is not implemented yet.
