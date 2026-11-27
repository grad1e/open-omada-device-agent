# Configuration Reference

Configuration is read from `OMADA_*` environment variables. `python-dotenv`
loads a local `.env` file automatically for development and lab use.

Copy the template:

```bash
cp .env.example .env
```

## Controller transport

| Variable | Default | Description |
| --- | --- | --- |
| `OMADA_CONTROLLER_HOST` | required | Controller hostname or IP |
| `OMADA_DISCOVERY_PORT` | `29810` | UDP discovery port |
| `OMADA_MANAGE_PORT` | `29814` | Default ECSP V2 management port |
| `OMADA_LOCAL_DISCOVERY_PORT` | `0` | Local UDP source port; `0` lets the OS choose |
| `OMADA_DISCOVERY_INTERVAL` | `5` | Discovery interval in seconds |
| `OMADA_TCP_TIMEOUT` | `15` | Management socket timeout |
| `OMADA_INFORM_INTERVAL` | `3` | Managed inform interval |
| `OMADA_RECONNECT_DELAY` | `3` | Delay between direct reconnect attempts |
| `OMADA_MANAGED_RECONNECT_ATTEMPTS` | `3` | Failures before managed rediscovery |

## Controller and site routing

| Variable | Description |
| --- | --- |
| `OMADA_CONTROLLER_ID` | Logical controller identifier used by the ECSP management session |
| `OMADA_SITE_ID` | Site-scoped discovery target and preferred External Portal `site` parameter |
| `OMADA_DEST_OMADAC_ID` | Low-level discovery destination override when no Site ID is used |

For the tested Controller 6.2 family, Controller IDs are typically 32
characters and Site IDs 24 characters.

## Device identity

| Variable | Default |
| --- | --- |
| `OMADA_DEVICE_MAC` | MAC of `OMADA_OPENWRT_LAN_BRIDGE` (`br-lan`) at first start, then the value saved in `OMADA_DEVICE_MAC_FILE`; `02:00:00:00:00:01` when that interface is missing. A set value may use `:`, `-` or no separator; an invalid one stops the agent |
| `OMADA_DEVICE_MAC_FILE` | `.omada-device-mac` (in the agent directory). Delete it to pick up a new bridge MAC |
| `OMADA_DEVICE_NAME` | `OpenOmada-AP` |
| `OMADA_DEVICE_MODEL` | `EAP110`. `auto` picks the closest Omada AP model from the radios (see below) |
| `OMADA_DEVICE_MODEL_VERSION` | `4.0` |
| `OMADA_DEVICE_HARDWARE_VERSION` | `4.0` |
| `OMADA_DEVICE_FIRMWARE_VERSION` | `5.0.4` |
| `OMADA_CUSTOMIZE_REGION` | `841` |
| `OMADA_DEVICE_IP` | `local` |

`OMADA_DEVICE_IP=local` (the default) advertises the address this host uses to
reach `OMADA_CONTROLLER_HOST`, found by a UDP route lookup that sends no
packet. It is re-evaluated for every inform, so a DHCP change is picked up.
If there is no route, `0.0.0.0` is advertised.

Set a concrete address to override it. `OMADA_DEVICE_IP=auto` performs a
best-effort public-IP lookup through `OMADA_PUBLIC_IP_LOOKUP_URL`, a
third-party service.

Controller 6.2.14.11 and 6.3.0.111 display the advertised value as-is in the
device list (Observed).

### Choosing the model automatically

`OMADA_DEVICE_MODEL=auto` reads the radios from `iw list` at start and picks
the Omada AP model that is closest to them, with the model and hardware
versions a real one sends at adoption (`OMADA_DEVICE_MODEL_VERSION` and
`OMADA_DEVICE_HARDWARE_VERSION` are then ignored). A model must have the same
bands. Among those the agent prefers one that claims nothing the hardware
cannot do (a newer Wi-Fi generation, more spatial streams, wider channels),
then the one that falls short of it least, then a plain model over an
`-Outdoor`/`-Wall`/... variant, then the closest transmit power. The log names
the choice: `OMADA_DEVICE_MODEL=auto: using EAP683 UR (model version 1.0, hardware version 1.0; exact match)`.
With a fixed model the agent logs the closest model instead, as a hint.

The models come from a table of a few facts per model, generated from
Controller 6.3.0.111's model templates (see NOTICE). Validated: the lab AP
(dual-band Wi-Fi 6, 4×4, 160 MHz) gets EAP683 UR, the model chosen by hand
before. Changing the model of an adopted AP needs a Forget and a new adoption
in the controller.

Wi-Fi 7 radios (an `EHT Iftypes: AP` block in `iw list`) and 320 MHz on
6 GHz (`320MHz in 6GHz Supported`) are recognised (Inferred: checked against
published `iw list` captures of Wi-Fi 7 cards, not yet on a Wi-Fi 7 OpenWrt
AP). A 2.4 GHz radio always counts as at most 40 MHz wide.

## Device Account authentication

| Variable | Description |
| --- | --- |
| `OMADA_DEVICE_USERNAME` | Optional explicit Device Account username |
| `OMADA_DEVICE_PASSWORD` | Device Account password; required for the current verification path |
| `OMADA_DEVICE_CIPHER_TYPE` | Current implementation expects `5` |

If the username is omitted, the agent requests it from the controller during
pre-connect.

**First adoption uses the factory credentials.** While the agent reports
`isFactory=true` (no managed state yet), the controller verifies it against the
factory default Device Account `admin` / `admin`, not the site Device Account.
After adoption the controller provisions the site Device Account, and later
reconnects verify with that password (Observed on Controller 6.2.14.11 and
6.3.0.111). For a first adoption, set `OMADA_DEVICE_USERNAME=admin` and
`OMADA_DEVICE_PASSWORD=admin`, then switch `OMADA_DEVICE_PASSWORD` to the site
Device Account password once the device shows as Connected. A mismatch shows in
the controller as "username or password incorrect" and in its log as
`device auth is error, adopt info is wrong`.

Never commit `.env`.

## TLS

| Variable | Default | Description |
| --- | --- | --- |
| `OMADA_TLS_VERIFY` | `false` | Verify the controller's TLS certificate |
| `OMADA_TLS_CA_FILE` | empty | Optional custom CA file |

For a trusted deployment:

```dotenv
OMADA_TLS_VERIFY=true
OMADA_TLS_CA_FILE=/etc/open-omada/controller-ca.pem
```

## Uplink telemetry

| Variable | Default | Description |
| --- | --- | --- |
| `OMADA_LAN_RATE` | `100` | Reported wired link rate |
| `OMADA_LAN_DUPLEX` | `1` | Duplex value used by the reference profile |
| `OMADA_LAN_PORT` | `LAN` | Uplink port label |

## AP platform features

| Variable | Default | Description |
| --- | --- | --- |
| `OMADA_PLATFORM` | `auto` | `auto`, `openwrt`, or `generic` capability detection |
| `OMADA_RADIO_BANDS` | `2g` | Comma-separated supported AP bands: `2g,5g,5g2,6g` |
| `OMADA_MAX_SSIDS` | `4` | Manual upper bound for SSIDs the platform adapter will accept. On OpenWrt, `iw list` can reduce this automatically when the radio does not support multiple AP interfaces. |
| `OMADA_CAP_WLAN` | auto on OpenWrt with `uci` | Enable WLAN/radio UCI reconciliation |
| `OMADA_CAP_WPA2_PSK` | auto on OpenWrt with `uci` | Accept WPA-Personal WLANs: WPA-PSK, WPA2-PSK and WPA/WPA2-PSK, with Auto (TKIP+AES) or AES |
| `OMADA_CAP_WPA3_PSK` | auto when `hostapd -vsae` succeeds | Accept WPA2-PSK/WPA3-SAE WLANs (`sae-mixed`) |
| `OMADA_CAP_80211R` | auto when `hostapd -v11r` succeeds | Accept 802.11r fast roaming (FT-PSK and FT-SAE) |
| `OMADA_CAP_80211K` | auto on OpenWrt with `uci` and hostapd installed | Accept the SSID's Load Balance setting and write `ieee80211k` |
| `OMADA_CAP_80211V` | auto on OpenWrt with `uci` when the hostapd binary has WNM (`wnm_sleep_mode`; full `wpad` builds, not `wpad-basic`) | Also write `bss_transition` for Load Balance. Forcing it on with `wpad-basic` leaves the radio down |
| `OMADA_CAP_SSID_VLAN` | `false` | Accept SSID VLAN mapping |
| `OMADA_CAP_DYNAMIC_VLAN` | `false` | Accept dynamic VLAN requests |
| `OMADA_CAP_MANAGEMENT_VLAN` | `false` | Accept management VLAN requests |
| `OMADA_CAP_PORTAL` | auto with OpenWrt + openNDS | Enable portal WLAN acceptance; explicit `false` disables portal acceptance |
| `OMADA_CAP_DHCP_TRACKING` | auto on OpenWrt with `ubus` | Include observed DHCP lease clients |
| `OMADA_CAP_OPTION82` | `false` | Accept DHCP Option 82 WLAN settings |
| `OMADA_CAP_LED` | path-based | Enable sysfs LED brightness/trigger writes |
| `OMADA_CAP_CLIENT_OPERATIONS` | auto on OpenWrt with `ubus` | Enable supported client control commands |
| `OMADA_CAP_CLIENT_RATE_LIMITS` | `false` | Enable nftables client rate-limit enforcement |
| `OMADA_CAP_IP_SETTINGS` | `false` | Apply Device Config → IP Settings to the LAN interface, with a 3-minute rollback if the controller is unreachable afterwards. In DHCP mode the fallback address is used after 30 s without a lease |
| `OMADA_CAP_SNMP` | `true` on OpenWrt when `snmpd` is installed | Apply Services → SNMP to `/etc/config/snmpd` |
| `OMADA_CAP_TIMEZONE` | `true` on OpenWrt with `uci` | Set OpenWrt's system time zone from the site time zone the controller sends at every connect |
| `OMADA_CAP_LLDP` | `true` on OpenWrt when `lldpd` and `lldpcli` are installed | Run LLDP with `lldpd` on the LAN bridge's wired ports when the controller turns it on, and report neighbours |
| `OMADA_CAP_ROGUE_AP_SCAN` | `true` on OpenWrt when `iw` lists the radios | Answer the controller's Interference Detection scan with `iw dev <ap> scan ap-force` |

## OpenWrt startup bootstrap

The agent runs an idempotent OpenWrt bootstrap before ECSP discovery when
`OMADA_OPENWRT_BOOTSTRAP=true` and the detected platform is OpenWrt. This is
where lab-only manual setup belongs.

| Variable | Default | Description |
| --- | --- | --- |
| `OMADA_OPENWRT_BOOTSTRAP` | `true` | Enable startup self-healing for OpenWrt prerequisites |
| `OMADA_OPENWRT_BOOTSTRAP_LAN` | `true` | Ensure the LAN bridge can exist even with only Wi-Fi ports |
| `OMADA_OPENWRT_LAN_INTERFACE` | `lan` | UCI network interface used by Omada WLANs |
| `OMADA_OPENWRT_LAN_BRIDGE` | `br-lan` | UCI bridge device used by OpenWrt AP interfaces and openNDS |
| `OMADA_OPENWRT_LAN_IPADDR` | `192.168.1.1/24` | LAN address used only when the bootstrap must create a missing LAN interface |
| `OMADA_OPENWRT_BOOTSTRAP_OPENNDS` | `true` | Enable and start openNDS when installed |
| `OMADA_OPENNDS_GATEWAY_PORT` | `2050` | openNDS local gateway port |
| `OMADA_OPENNDS_GATEWAY_NAME` | device name | openNDS gateway name; empty uses `OMADA_DEVICE_NAME` |
| `OMADA_OPENNDS_INTERFACE` | `OMADA_OPENWRT_LAN_BRIDGE` | Bridge openNDS guards. The agent refuses to start openNDS on the bridge that carries the default route (or, without one, the route to the controller) |
| `OMADA_PORTAL_NETWORK` | empty | UCI network that portal SSIDs join, for example `guest`; empty keeps them on `lan` |
| `OMADA_SITE_NAME` | empty | Human-readable fallback for External Portal redirects when no site ID is available |
| `OMADA_OPENWRT_ENABLE_WAN_MANAGEMENT` | `false` | Lab-only opt-in to open SSH, LuCI HTTP, and LuCI HTTPS from the WAN zone |
| `OMADA_OPENWRT_WAN_ZONE` | `wan` | Firewall zone used by the WAN management opt-in rules |
| `OMADA_WIFI_COUNTRY` | empty | ISO 3166 two-letter code set as `wireless.radioX.country` on every radio. The controller never sends the site country to the AP; without a country OpenWrt uses the restrictive world domain and controller-selected channels can be refused. Empty keeps the current UCI value and logs a warning if a radio has none |

The LAN bootstrap sets `bridge_empty=1` on the bridge device. This is required
on Wi-Fi-only OpenWrt devices where `br-lan` otherwise does not exist until a
radio interface is already attached, which prevents hostapd/openNDS from
starting cleanly.

## OpenWrt targets

| Variable | Default | Description |
| --- | --- | --- |
| `OMADA_MANAGEMENT_VLAN_INTERFACE` | empty | UCI network interface to update for management VLAN |
| `OMADA_MANAGEMENT_VLAN_DEVICE` | empty | Base OpenWrt device, for example `br-lan` |
| `OMADA_HOSTAPD_UBUS_IFACE` | empty | hostapd ubus object suffix for reconnect/deauth, for example `wlan0` |
| `OMADA_CLIENT_BLOCK_INTERFACE` | empty | Bridge/interface used by nftables client block rules |
| `OMADA_CLIENT_RATE_LIMIT_INTERFACE` | empty | Bridge/interface used by nftables per-client rate-limit rules |
| `OMADA_PORTAL_INTERFACE` | empty | Fallback nftables-only portal interface; unused when openNDS is installed |
| `OMADA_PORTAL_REDIRECT_PORT` | `8080` | Fallback nftables-only local HTTP redirect port; unused when openNDS is installed |

Unsupported controller keys are intentionally rejected with a local
`SET_RESPONSE.errcode=1`.

## Captive portal on OpenWrt

Use `openNDS` as the OpenWrt captive portal engine. The built-in nftables
adapter remains only as a fallback for lab setups without openNDS.

For OpenWrt 25 snapshots:

```bash
apk update
apk add opennds dnsmasq-full conntrack
```

For older OpenWrt package feeds:

```bash
opkg update
opkg install opennds dnsmasq-full conntrack
```

`scripts/deploy-openwrt.sh` runs these installs (and adds Python,
`snmpd` and `lldpd`) unless given `--no-packages`. It leaves openNDS switched off
(`enabled '0'`); the agent turns it on when the controller configures a
portal.

openNDS treats every device on the bridge it guards as a portal client,
including the upstream router. On an AP that is not the router (a "dumb AP",
uplink on `br-lan`), guarding `br-lan` cuts the AP off from its gateway and the
controller, so the agent refuses to do that. Give portal SSIDs a routed guest
network instead: its own bridge and subnet, DHCP on the AP, a firewall zone
forwarding to `lan`, and masquerading of the guest subnet. Then set
`OMADA_PORTAL_NETWORK=guest` and `OMADA_OPENNDS_INTERFACE=br-guest`.

```bash
uci set network.br_guest=device
uci set network.br_guest.type='bridge'
uci set network.br_guest.name='br-guest'
uci set network.br_guest.bridge_empty='1'
uci set network.guest=interface
uci set network.guest.proto='static'
uci set network.guest.device='br-guest'
uci set network.guest.ipaddr='10.77.0.1/24'
uci set dhcp.guest=dhcp
uci set dhcp.guest.interface='guest'
uci set dhcp.guest.start='100'
uci set dhcp.guest.limit='100'
uci set firewall.guest=zone
uci set firewall.guest.name='guest'
uci set firewall.guest.network='guest'
uci set firewall.guest.input='REJECT'
uci set firewall.guest.output='ACCEPT'
uci set firewall.guest.forward='REJECT'
uci set firewall.guest_lan=forwarding
uci set firewall.guest_lan.src='guest'
uci set firewall.guest_lan.dest='lan'
uci set firewall.@zone[0].masq='1'
uci add_list firewall.@zone[0].masq_src='10.77.0.0/24'
# Also allow DHCP (udp 67-68), DNS (53) and the openNDS port (tcp 2050)
# from the guest zone to the AP.
uci commit; /etc/init.d/network reload; /etc/init.d/firewall reload
```

Minimal UCI example:

```bash
uci set opennds.@opennds[0].enabled='1'
uci set opennds.@opennds[0].gatewayinterface='br-lan'
uci set opennds.@opennds[0].gatewayport='2050'
uci set opennds.@opennds[0].gatewayfqdn='disable'
uci commit opennds
/etc/init.d/opennds restart
```

With openNDS installed, the agent:

- reports portal client state from `ndsctl json`;
- applies `portalFreePolicyConfig` URL rules as openNDS walled-garden FQDNs;
- applies `portalFreePolicyConfig` IP rules as openNDS preauthenticated user
  rules;
- writes `/usr/lib/opennds/theme_openomada_redirect.sh` and sets
  `login_option_enabled=3` when Omada provides a portal URL through
  `portalConfigList` or a `/portal/...` free-policy URL;
- redirects EAP clients to an external portal server with the TP-Link
  External Portal query contract: `clientMac`, `clientIp`, `t`, `site`,
  `redirectUrl`, `apMac`, `ssidName`, and `radioId`;
- redirects clients to the controller's built-in portal (`/portal/entry`)
  with the names that page reads: `cid`, `ap`, `ssid`, `rid`, `t`, `u`, and
  `clientIp` (Validated: Controller 6.3.0.111 answers 400 with the external
  names and serves the login page with these);
- lets clients reach the login page host before login (walled-garden FQDN,
  or a preauthenticated rule for an IP address);
- sets `allow_preemptive_authentication=0` so clients use the classic HTTP
  redirect path instead of stopping at the openNDS RFC8910 status page;
- sets `gatewayfqdn=disable` so the openNDS preauth redirect uses the gateway
  IP instead of the local-only `status.client` hostname. This avoids breaking
  captive portal flow on clients that ignore DHCP DNS and use a custom resolver;
- applies `clientConfig.unauth=false` and controller portal logins
  (`EVENT_PORTAL_AUTH`) with `ndsctl auth`. The session length is the
  login's `end - start`, so it follows the portal's Authentication Timeout;
  without those fields openNDS uses its own `sessiontimeout`;
- applies `clientConfig.unauth=true` with `ndsctl deauth`;
- clears conntrack entries for the client IP after deauth when `conntrack` is
  installed;
- does not install its own `inet openomada_portal` table.

The generated ThemeSpec derives `clientMac`, `clientIp`, and `originurl` from
openNDS FAS variables, uses `OMADA_DEVICE_MAC` for `apMac`, falls back to
`iw dev <clientif> info` for SSID/radio detection, and uses `OMADA_SITE_ID` as
the `site` parameter when available. `OMADA_SITE_NAME` is only a fallback. MAC
addresses are emitted in Omada portal form (`AA-BB-CC-DD-EE-FF`), timestamps are
seconds, and query values use form encoding (`+` for spaces).

## DHCP/client tracking

| Variable | Default | Description |
| --- | --- | --- |
| `OMADA_DHCP_LEASE_FILE` | `/tmp/dhcp.leases` | dnsmasq lease file used to report real IP/hostname client data |
| `OMADA_CLIENT_SNOOP_INTERFACES` | `auto` | Bridges to listen on for client IPs and names (`auto` = every bridge, `off` disables, or a comma-separated list) |

A bridge-only AP has no leases of its own, so the agent also listens (receive
only) on its bridges. A kernel filter passes just DHCP, ARP, IPv6 neighbour
discovery and mDNS packets; from those it learns each client's IPv4 and IPv6
addresses and its DHCP host name (or mDNS `.local` name). Addresses age out
after an hour without traffic, names after a day. A client that was already
connected shows its name after its next DHCP renewal or reconnect.

If the lease file is absent, DHCP enrichment is skipped. DHCP leases and
openNDS sessions are metadata sources only: production inform reports a client
only when the same MAC is currently present in the latest hostapd association
list. This prevents disconnected clients from lingering as active Omada clients.

## LED control

| Variable | Default | Description |
| --- | --- | --- |
| `OMADA_LED_BRIGHTNESS_PATH` | empty | sysfs brightness file for `led.enable` |
| `OMADA_LED_ON_VALUE` | `1` | Value written when Controller enables the LED |
| `OMADA_LED_OFF_VALUE` | `0` | Value written when Controller disables the LED |
| `OMADA_LED_TRIGGER_PATH` | empty | sysfs trigger file for `led.locate` |
| `OMADA_LED_LOCATE_TRIGGER` | `timer` | Trigger value while locate is active |
| `OMADA_LED_DEFAULT_TRIGGER` | `none` | Trigger value restored when locate stops |

On OpenWrt, use the LED the image marks as its running status light. The
device-tree alias names it: `cat /proc/device-tree/aliases/led-running` gives a
node such as `/leds/led-0`, and `/sys/class/leds/` lists it by color, for
example on a JioRouter AX6000 (Observed):

```sh
OMADA_LED_BRIGHTNESS_PATH=/sys/class/leds/red:status/brightness
OMADA_LED_TRIGGER_PATH=/sys/class/leds/red:status/trigger
```

When Locate stops, the trigger is restored first and then the last On/Off
setting, because Linux turns an LED off when its trigger is set to `none`.

`wifiControlLed` is parsed but rejected because it represents hardware button
semantics, not the LED brightness/locate state itself.

## State file

The default state file is derived from the device MAC:

```text
.omada-agent-state-<mac without colons>.json   # e.g. ...-020000000001.json
```

Override with `OMADA_STATE_FILE` when needed. The file is Git-ignored by
default.

On OpenWrt the agent also keeps `.omada-wireless-state-<mac>.json`: the MAC
filter groups, WLAN schedules and their SSID links as last applied. The
controller sends these changes one rule at a time, so the agent needs them after a restart. Override
with `OMADA_WIRELESS_STATE_FILE`.
