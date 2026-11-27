# Changelog

All notable project changes will be documented in this file.

## Unreleased

### Added

- `docs/protocol-status.md`: a "Parked work" section listing known gaps
  that are deliberately on hold, why, and what would unblock them, and the
  tests that still need a real EAP or switch.
- `OMADA_DEVICE_MODEL=auto` picks the Omada AP model closest to the AP's
  radios (bands, spatial streams, widest channel, Wi-Fi generation, transmit
  power) from a table generated from the controller's model templates. With a
  fixed model the closest one is logged as a hint. The table holds only those
  facts; `scripts/export-omada-models.sh` and `scripts/gen_omada_models.py`
  regenerate it, and NOTICE and AGENTS.md rule 1 describe what it may hold.
- README: "Install on an OpenWrt access point", a step-by-step guide from a
  freshly reset OpenWrt device to an adopted AP.
- `scripts/deploy-openwrt.sh --env FILE` copies a local settings file to the AP
  as `.env`. The script now installs `python3`, `python3-dotenv` and
  `python3-psutil`, writes the `/usr/bin/open-omada-agent` launcher, and
  installs and enables the boot service when it is missing, so a fresh
  OpenWrt device needs no manual setup.

- Device Config → IP Settings (opt-in with `OMADA_CAP_IP_SETTINGS=true`):
  DHCP or a static address, netmask, gateway and DNS from SET key
  `lanSetting` are written to the OpenWrt LAN interface. The change runs after
  the SET is answered. A detached watchdog restores the previous
  `/etc/config/network` after 3 minutes unless the agent reaches the
  controller from the new address. The agent reconnects from the new address,
  and does not retry a change that was rolled back until the next reboot. The
  current address is reported to the controller at adoption. In DHCP mode
  the controller's fallback address is added after 30 seconds without a
  DHCP lease, with a default route through the fallback gateway when there is
  none, and removed when a lease arrives.
- Site Network Config → SSH is documented as not applied by design: the agent
  leaves dropbear alone.
- Site time zone: the agent sets OpenWrt's system time zone from the site
  time zone the controller sends at every connect and when the site's time
  settings are saved, as a UTC offset with the site's daylight-saving rules
  (for example `<+0530>-5:30`, or `<+00>0<+01>-1,M3.5.0/1,M10.5.0` for London).
  `OMADA_CAP_TIMEZONE=false` turns it off.
- Device Config → Services → LLDP: when `lldpd` is installed the agent
  advertises LLDP support, runs LLDP (only LLDP, not CDP/FDP/SONMP/EDP) on the
  LAN bridge's wired ports while the controller has it on (without the
  package's placeholder LLDP-MED location), stops lldpd when
  it is off, and reports the LLDP neighbours it hears in the inform.
  `scripts/deploy-openwrt.sh` installs `lldpd` and leaves it stopped until
  LLDP is enabled. `OMADA_CAP_LLDP=false` turns it off.
- Device Config → Services → SNMP and the site SNMP settings: the agent
  advertises the `snmp` component when net-snmp's `snmpd` is installed and
  writes `/etc/config/snmpd` from SET key `snmp`: Location, Contact, the
  read-only v1/v2c community, and a read-only SNMPv3 user (MD5/SHA auth,
  DES/AES privacy; the agent also lists component `service` 1.1 so the
  controller sends the v3 security level and modes). snmpd runs only while
  v1/v2c or v3 is on.
  `OMADA_CAP_SNMP=false` turns it off.
- `scripts/deploy-openwrt.sh` installs the OpenWrt packages the agent uses
  (`snmpd`, `opennds`, `dnsmasq-full`, `conntrack`) with `apk` or `opkg`,
  swapping `dnsmasq` for `dnsmasq-full` safely and leaving snmpd stopped until
  SNMP is enabled. `--no-packages` skips this.
- Tools → Network Check: Ping, Traceroute, DNS Lookup and ARP Table. The agent
  advertises the `ping`/`traceroute`/`dnslookup`/`arptable` components, runs
  BusyBox `ping`/`traceroute`/`nslookup` (or reads `/proc/net/arp`) in the
  background, and streams the output line by line as TROUBLESHOOTING notifies
  (plain `NOTIFY_REQUEST`, base64 `msg`). `OMADA_CAP_NETWORK_CHECK=false`
  turns it off.
- Client IP addresses and names on a bridge-only AP: the agent listens
  (receive only) on its bridges and learns each Wi-Fi client's IPv4, IPv6 and
  host name from DHCP requests, ARP, IPv6 neighbour discovery and mDNS. A
  kernel BPF filter passes only those packets. Learned details only enrich
  clients hostapd reports as associated. `OMADA_CLIENT_SNOOP_INTERFACES`
  (default `auto`, every bridge) picks the bridges or turns it `off`.
- Interference Detection scan: the controller's "Scan" button (SET key
  `rogueApScan`) runs `iw dev <ap> scan ap-force` on each band in the
  background, and the next inform reports the neighbour APs in `rogueApList`
  (BSSID, SSID, channel, security, beacon interval, signal, Wi-Fi mode). The
  agent advertises the `rogueAp`/`rogueApInform` components when `iw` can
  scan; `OMADA_CAP_ROGUE_AP_SCAN=false` turns it off. `rogueApGlobal` and
  `rogueApSetting` are accepted without local changes.
- Wireless Mode per radio: `wirelessBasic_*.wirelessMode` picks the htmode
  family (b/g → `NOHT`, b/g/n and a/n → `HT`, a/n/ac → `VHT`, b/g/n/ax and
  a/n/ac/ax → `HE`), capped at what the radio supports; 802.11n modes stop at
  40 MHz. Auto (sent as no `wirelessMode`) uses the radio's newest standard
  again. Each radio SET is logged with its mode, width, channel and Tx power.
- Wireless → Advanced per radio: Maximum Associated Clients (`loadBalance_*`)
  sets UCI `maxassoc`, RSSI Threshold (`rssi_*`) sets `rssi_reject_assoc_rssi`,
  and U-APSD (`qosConfig_*`) sets `uapsd` on the agent's SSIDs (kept on the
  radio as `openomada_uapsd` so rebuilt SSIDs keep it). Turning a limit off
  removes the option. The agent advertises `lb`, `rssiFilter`, `qos` and
  `wlanAdv` with WLAN support. OFDMA (`wirelessAdv_*`) is accepted but not
  applied: OpenWrt has no switch for it.
- `OMADA_PORTAL_NETWORK` puts portal SSIDs on their own UCI network and
  `OMADA_OPENNDS_INTERFACE` points openNDS at its bridge, so the captive portal
  works on an AP that is not the router.
- Adoption sends `devCap` with antenna count (`mimo2G`/`mimo5G`), supported
  wireless modes and OFDMA bands, using the bitmask values of real EAPs of the
  same Wi-Fi generation.
- DFS channels and 160 MHz widths are advertised to the controller when
  `iw reg get` reports a DFS region (FCC, ETSI or JP). Under `DFS-UNSET`
  (for example country IN in wireless-regdb) they stay hidden, because
  hostapd cannot run radar detection and would disable the radio.
- OpenWrt procd service (`scripts/openwrt/open-omada-agent.init`) to start
  the agent at boot and restart it after a crash. Output goes to the system
  log (`logread -e open_omada`, tagged `open-omada-agent`), not a file in
  `/tmp` (RAM on OpenWrt). `scripts/deploy-openwrt.sh` restarts that service
  when it is enabled.
- `--workdir DIR` changes to DIR before the agent loads `.env` and its managed
  state file; the procd service uses it to start the agent directly.
- AP `SET_REQUEST` domain parsing for radios, SSIDs, VLAN, portal policy, LED,
  and Wi-Fi control LED keys.
- OpenWrt UCI reconciliation for supported radio/SSID/PSK and opt-in SSID VLAN
  configuration.
- Platform capability detection to prevent unsupported AP features from being
  acknowledged as applied.
- DHCP lease backed client inform reporting.
- Captive portal session lifecycle, nftables policy rendering, and minimal
  RADIUS PAP authentication primitives.
- OpenWrt wireless telemetry for `wSettings_*` and `ssidStats_*` inform keys.
- `FORGET_REQUEST` and `FORGET_REQUEST_NO_RESET` handling.
- Optional sysfs LED brightness adapter for `led.enable`.

### Changed

- `scripts/deploy-openwrt.sh` needs `--host`; the default install path is
  `/root/open-omada-device-agent`. It checks host keys against your
  `known_hosts` (saving a new AP's key) instead of a throwaway file that
  skipped the check, and reuses one SSH connection, so a password is asked
  for once.
- The device MAC now defaults to the LAN bridge MAC (`br-lan`, or
  `OMADA_OPENWRT_LAN_BRIDGE`) instead of `02:00:00:00:00:01`. A controller
  "Use Fixed IP Address" reservation then matches the AP's DHCP requests, and
  two unconfigured APs no longer share one MAC. The first MAC is saved to
  `.omada-device-mac`, so an AP whose hardware picks a random MAC at every
  boot keeps one identity; the agent warns that such an AP needs a fixed
  `macaddr` for DHCP reservations. An AP that ran without `OMADA_DEVICE_MAC`
  shows up as a new device after the upgrade; set
  `OMADA_DEVICE_MAC=02:00:00:00:00:01` to keep the old identity.
- `OMADA_DEVICE_IP` defaults to `local`: the agent advertises the address it
  uses to reach the controller (a UDP route lookup that sends no packet,
  IPv4 or IPv6), re-checked every 60 s. Previously `0.0.0.0` was shown unless the address
  was set by hand. Fixed addresses and `auto` (public lookup) still work.
- Unsupported AP `SET_REQUEST` keys now return a local config error instead of
  being silently acknowledged.

### Fixed

- A portal SSID is refused before anything is written when openNDS would run
  on the AP's uplink. The SSID used to be written first and the SET refused
  afterwards, leaving the SSID broadcasting with no portal in front of it.
  An unset openNDS `gatewayinterface` now counts as `br-lan`, openNDS's
  default, instead of skipping the uplink check.
- A SET with an unsupported key no longer drops the supported changes in the
  same SET. Example: removing a MAC filter also sends `macFilterAssoc`, so
  the SSID change in that SET (such as turning client isolation off) was
  skipped until the next full sync. The agent now applies the supported
  keys and still reports the SET as failed, naming the unsupported keys.
- A full config sync no longer fails on an AP without a portal SSID. Every
  full sync carries `portalFreePolicyConfig`; when openNDS's gateway is the
  AP's uplink, the agent now skips openNDS (and logs why) instead of
  reporting the whole SET as failed. A portal SSID on that AP is still
  refused.
- A failed `ip addr add` for the DHCP fallback address is logged and tried
  again after 30 s, instead of being recorded as in use. When the fallback
  address is changed while in use, the swap happens at once (also after an
  agent restart) and the controller is told if the old address is gone.
- An AP without a default route (a network with no internet) can run openNDS
  on a routed guest bridge: the route to the controller then identifies the
  uplink. This needs `OMADA_CONTROLLER_HOST` as an IPv4 address; while the
  uplink is unknown, openNDS is no longer started when a portal SSID arrives
  (it used to start without a default route).
- `--once` exits with an error when the discovery cannot be sent (no route)
  instead of retrying forever.
- Client snooping no longer logs an error with a traceback when the LAN
  bridge goes down for a moment during a network reload.
- After an IP Settings change or a DHCP fallback switch, the controller is
  shown the AP's new address at once instead of the old one for up to a
  minute.
- openNDS is no longer enabled on the LAN bridge when the AP has no default
  route (DHCP still waiting, link down). The uplink is unknown then, and
  openNDS on the uplink bridge cuts the AP off from its gateway and the
  controller. It stays off until a default route shows the interface is safe.
- Discovery no longer crashes the agent when the AP briefly has no route to
  the controller (for example right after an address change); it logs and
  retries.
- `OMADA_DEVICE_MAC` accepts the label form (`02-AA-BB-CC-DD-EE` or
  `02AABBCCDDEE`) and is lower-cased; an invalid value now stops the agent
  with a clear error instead of being sent to the controller as is.
- At boot on OpenWrt the agent waits up to 10 seconds for the LAN bridge
  before picking the device MAC, and warns when it has to use the shared
  placeholder MAC.
- IP Settings: when the watchdog cannot restore `/etc/config/network`, it
  keeps the backup and logs "IP Settings rollback FAILED" instead of marking
  the change as rolled back and deleting the backup.
- The random-MAC startup warning recommends pinning the MAC on the device's
  label instead of the random MAC from the first boot, and says how to
  re-adopt when the MAC changes.
- Re-running `scripts/deploy-openwrt.sh` now updates
  `/etc/init.d/open-omada-agent` when the repository's version changed, so
  fixes to the boot service reach installed APs. A local `AGENT_ARGS` line
  is kept.
- An access point without a portal no longer logs "openNDS gateway interface
  br-lan carries the default route" at every start. openNDS stays off on such
  an AP as before; the warning now appears only if the controller sends a
  portal SSID.
- `scripts/deploy-openwrt.sh` on a fresh OpenWrt device: openNDS started on
  `br-lan` as soon as it was installed and cut the AP off from its network,
  so the remaining package downloads failed. It is now installed last and
  left switched off.
- An SSID deleted in the controller kept broadcasting. Each `ssid_*` list in a
  SET is the band's complete SSID set, so the agent now deletes the SSIDs it
  created on that band that the list no longer has. SSIDs made by hand in
  OpenWrt are left alone.
- OFDMA: the controller's OFDMA checkbox is hidden (`radioCap.ofdma=0`), since
  OpenWrt has no switch to turn OFDMA off on Wi-Fi 6 radios, and
  `radioCap.ofdmaEnable` now reports it on for 802.11ax radios instead of off.
- Tx Power reaches what the radio can do: `radioCap.maxPow` and each
  channel's `mPow` now come from the per-channel limit `iw phy` reports
  (30 dBm on the Jio under IN). It was fixed at 20 dBm, so Custom stopped at
  20 and High/Auto sent 20. The old 20/23 dBm values remain the fallback.
- A `SET_REQUEST` without `configVersion`/`configVersionInc` (one-off commands
  such as `rogueApScan`) keeps the current version. It used to drop the
  managed session, which then re-adopted.
- A repeated portal login for a client openNDS already authenticated is no
  longer reported as an error (the controller can send the login twice).
- Portal logout clears the client's conntrack entries again: `ndsctl json
  <mac>` prints the client at the top level, which the agent did not read.
- Portal logins now last as long as the portal's Authentication Timeout
  (for example 8 hours). The agent passes the login's `end - start` to
  `ndsctl auth` instead of leaving openNDS at its 24-hour default.
- Redirects to the controller's built-in portal page use the query names it
  reads (`cid`, `ap`, `ssid`, `rid`, `t`, `u`). The External Portal names made
  Controller 6.3.0.111 answer HTTP 400 ("Redirect device is not valid").
- `portalAuthClients` lists only clients that have logged in. Clients still
  waiting at the portal were listed too, and Controller 6.3.0.111 logged
  "is PENDING but informed as portalAuthed" on every inform.
- Portal clients can reach the login page before they log in: its host is
  added to the openNDS walled garden (hostname) or preauthenticated rules (IP
  and port). On a routed guest network openNDS refused the controller's
  `/portal/entry` page.
- openNDS is started when a portal SSID is configured and it is not running.
  On a guest bridge it exits until the first portal SSID brings the bridge
  up, and a "No Authentication" portal SET carries no portal policy that
  would restart it.
- The agent no longer starts openNDS on the bridge that carries the default
  route. On an AP whose uplink router is on `br-lan`, openNDS treated the
  router as an unauthenticated client and the AP lost its controller
  connection; startup now warns and portal SETs return an error instead.
- `--once` exits after one discovery/adoption cycle again; the Forget
  recovery loop kept restarting discovery.
- After an agent restart, stopping Locate no longer turns the LED off: the
  On/Off state is read back from sysfs when no SET has set it yet.
- Channel lists carry the band bits every real EAP sends: 2.4 GHz `0xe0`
  (CCK, OFDM, 2 GHz), 5 GHz `0x140` (OFDM, 5 GHz), 6 GHz `0x300`. They match
  the Qualcomm/net80211 channel flags and are identical across all EAP model
  templates in Controller 6.3.0.111, Wi-Fi 4 to Wi-Fi 7.
- The LED stays on after Locate stops. Restoring the `none` trigger turned
  the LED off (Linux resets brightness), so the last On/Off setting is now
  written after the trigger, and before it when Locate starts.
- `wSettings_*` report the channel and width the radio runs on (from
  `iw dev <if> info`, e.g. `"6"`/`"20MHz"`) instead of `"0"` for Auto and the
  configured htmode, plus `rdMode` such as `"a/n/ac/ax mixed"`. The controller
  showed Channel 0 and Mode "802.11undefined".
- Clients without a signal reading are left out of the inform until hostapd
  reports one (a moment after they re-associate). Controller 6.3.0.111 client
  insights threw `NullPointerException` on a client without `rssi`.
- Inform stats carry the fields Controller 6.3.0.111 unboxes: `ssidStats_*.id`
  (the controller's SSID id, stored as uci `openomada_ssid_id`; `-1` for SSIDs
  it does not manage) and `wSettings_*` `busyUti`/`txUti`/`rxUti`/`interUti`
  from `iw survey dump`. Missing values threw `NullPointerException` in the
  controller's SSID statistics and AP health checks on every inform.
- `deviceInfo.upTime` is sent as `"<days> days HH:MM:SS"`. Bare seconds made
  Controller 6.3.0.111 throw `NoSuchElementException` in its stats parser on
  every inform.
- `controllerSetting` (portal logout domain, sent after every Site Settings
  save) is accepted as passive config instead of failing the `SET_REQUEST`.
- Auto channel lists keep only channels the radio can use now. A controller
  `channelRange`, or a `wireless.radioX.channels` list left over from another
  country or DFS state, could make ACS pick a radar channel that hostapd
  refuses, disabling the radio. Startup now prunes stale lists as well.
- After changing the Wi-Fi country at startup, the agent waits for the
  kernel to apply it before reading radio capabilities; it used to advertise
  the previous country's channels until the next restart.
- `--no-adopt` now exits after the first `PRE_ADOPT_REQUEST`, as its help text
  says, instead of continuing discovery forever.
- `wSettings_*.txPower` is reported as `"<n>dBm"`; a bare number made
  Controller 6.2/6.3 drop every inform and show the AP as "Heartbeat Missed".
- Omada `chanWidth` enum codes (2=20, 3=40, 5=80, 7=160 MHz; Auto 4=20/40 and 6=20/40/80) are mapped to
  OpenWrt `htmode`, keeping the radio's HE/VHT/HT family instead of forcing HT20.
- `lanInfo.rate` is reported as an integer string (`"100"`, not `"100.0"`);
  the controller topology service crashed parsing it, showing
  "Something went wrong" when saving device settings.
- Radio capabilities (Wi-Fi 5/6 support, spatial streams, 40/80 MHz channel
  flags) are read from `iw list` instead of being hardcoded as 802.11n 1x1, so
  the controller offers 80 MHz on capable radios. Falls back to the previous
  static profile when `iw` is unavailable. DFS channels are not advertised yet.
- Channel "Auto" is written as UCI `channel='auto'` with `channels` limited to
  the controller's `channelRange`, instead of `channel='0'` with no limit, which
  let OpenWrt ACS pick a DFS channel the regulatory domain could not run and
  silently disabled the radio.
- `OMADA_WIFI_COUNTRY` sets the regulatory country on every radio at startup
  (the controller never sends one), and a warning is logged when radios have no
  country. Channels the regulatory domain marks "no IR" are no longer
  advertised to the controller.
- One Adopt click no longer starts several adoption attempts: PRE_ADOPT_REQUEST
  retransmissions within 5 s of a finished attempt are ignored. The extra
  attempts were also what produced the misleading "controller did not return a
  username" error after a rejected password.
- A controller Forget no longer needs an agent restart: discovery restarts as
  a factory device (`isFactory=true`) instead of managed rediscovery, which the
  controller listed as "managed by others". A Forget right after a fresh
  adoption no longer makes the agent process exit.
- Channel capability flags include the base 20 MHz bit, so the controller
  offers channels for a 20 MHz width.
- `led.enable` no longer fails the whole SET_REQUEST when LED control is not
  configured; it is treated as passive.

### Documentation

- Shorter README aimed at new users: what the project does, that it is built
  and tested on the JioRouter AX6000 (JIDU6101) with OpenWrt 25.12.6 and
  welcomes pull requests for other devices, what works, a four-step
  install summary, and a prompt for setting it up with an AI coding agent.
  The full install guide moved to
  `docs/install-openwrt.md` (with JIDU6101 notes: label MAC pinning, LED
  paths), the captive portal details to `docs/captive-portal.md`, the
  feature table to `docs/protocol-status.md`, and running from source to
  `docs/development.md`.
- The project now lives at
  [grad1e/open-omada-device-agent](https://github.com/grad1e/open-omada-device-agent),
  a fork of the upstream repository with `develop` as its main branch.
  CONTRIBUTING.md covers AI-assisted changes and hardware testing,
  SECURITY.md covers sensitive logs and features that change the AP's
  network, and AGENTS.md (read by `CLAUDE.md`) is an English guide for
  coding agents.
- First adoption verifies against factory `admin` / `admin`; the site Device
  Account applies after adoption. `OMADA_DEVICE_IP` is displayed as-is.
- Reworked ECSP protocol and lifecycle documentation around Mermaid sequence diagrams for clearer transport, ordering, authentication, configuration, reconnect, and managed-rediscovery flows.

## 0.1.0-alpha.1 - 2026-08-22

Initial Open Omada public-alpha preparation.

### Included

- ECSP V2 length-prefixed JSON framing.
- Site-scoped UDP discovery.
- TLS management channel.
- Legacy Device Account mutual verification.
- Device/system negotiation and initial sync.
- Conservative AP component advertisement.
- Periodic managed informs.
- Absolute and incremental `SET_REQUEST` acknowledgements.
- Persistent non-secret reconnect state.
- Direct managed reconnect and stale-context managed rediscovery.
- Python package/CLI layout, tests, CI, and protocol documentation.
