# Protocol Support Matrix

## Features at a glance

The reference implementation targets the ECSP V2 family. It was first built
against Omada Network Application `6.2.14.11` with an EAP110 v4-compatible AP
profile, and has since been validated against Controller `6.3.0.111` with a
dual-band Wi-Fi 6 OpenWrt AP advertised as an EAP model of the same generation
(`OMADA_DEVICE_MODEL`, or `auto` to pick the closest model from the radios;
see the [configuration reference](configuration.md#device-identity)).

| Area | Status |
| --- | --- |
| UDP discovery / site-scoped discovery | ✅ Working |
| `PRE_ADOPT_REQUEST` handling | ✅ Working |
| TLS management channel | ✅ Working |
| Legacy V2 mutual Device Account verification | ✅ Working |
| Device/system negotiation | ✅ Working |
| Initial sync | ✅ Working |
| Managed state persistence | ✅ Working |
| Restart without another manual Adopt | ✅ Working |
| Managed rediscovery / stale-context recovery | ✅ Working |
| Periodic device informs | ✅ Working; telemetry in the formats Controller 6.3 parses |
| Device IP shown in the controller | ✅ `OMADA_DEVICE_IP=local` reports the address used to reach the controller (IPv4 or IPv6) |
| Radio capability advertisement | ✅ From `iw list`: channels, widths up to 160 MHz, antennas, wireless modes; DFS channels when the regulatory domain supports radar detection |
| `SET_REQUEST` acknowledgement | 🟡 Partial; rejects unsupported keys |
| Max clients, RSSI threshold, U-APSD | 🟡 Per radio from Wireless → Advanced; OFDMA is always on (checkbox hidden; OpenWrt has no switch for it) |
| Network Check (Tools) | 🟡 Ping, Traceroute, DNS Lookup and ARP Table run on the AP with BusyBox; output streams into the controller's Device Output |
| IP Settings (opt-in) | 🟡 DHCP (with fallback address) or static LAN address, gateway and DNS, with automatic rollback if the controller becomes unreachable; enable with `OMADA_CAP_IP_SETTINGS=true` |
| SNMP (Services) | 🟡 Location, Contact, v1/v2c community and a read-only SNMPv3 user written to OpenWrt's `snmpd`; snmpd runs only while SNMP is on |
| Site time zone | 🟡 The site's time zone and daylight saving become OpenWrt's system time zone (as a UTC offset with daylight-saving rules; zone names are not sent) |
| LLDP (Services) | 🟡 `lldpd` runs LLDP on the LAN ports while LLDP is on; neighbours are reported to the controller |
| Interference Detection scan | 🟡 On-demand neighbour scan with `iw`; shows in Statistics → Interference Detection |
| Radio/SSID configuration | 🟡 OpenWrt UCI adapter: SSIDs, wireless mode, channel, width (20–160 MHz), Tx power up to the radio's limit, Auto channel range, Wi-Fi country (`OMADA_WIFI_COUNTRY`) |
| SSID VLAN configuration | 🟡 Opt-in OpenWrt UCI adapter |
| Management VLAN configuration | 🟡 Opt-in OpenWrt UCI adapter |
| Client IP/name reporting | 🟡 hostapd association is the source of truth; IPs and names come from DHCP leases, openNDS and passive DHCP/ARP/NDP/mDNS listening on the bridges |
| OpenWrt radio/SSID telemetry | 🟡 Live channel, width and mode, per-SSID stats and channel utilisation via `ubus`, hostapd and `iw` |
| Client operations and rate limits | 🟡 Optional `ubus`/nftables adapters |
| LED enable/disable/locate | 🟡 Optional sysfs adapter; state survives Locate and agent restarts |
| FORGET / forget-no-reset response | ✅ Working |
| Captive portal sessions/enforcement | 🟡 openNDS on a routed guest network; controller built-in portal and External Portal redirects, free-policy walled garden, session length and client auth state are wired; guest logout is not |
| Portal RADIUS authentication | 🟡 Library support; not wired to HTTP portal flow |
| `GET_REQUEST` | 🟡 Defensive unsupported-key responses |
| Notify families | 🟡 Defensive unsupported replies |
| Report family | 🚧 Envelope and protobuf DTOs identified; `reportData` serializer not implemented |
| Switch/gateway/OLT device profiles | 🚧 Planned |

The capability table is intentionally conservative. The agent advertises only
components whose lifecycle is currently understood well enough not to mislead
the controller. On OpenWrt, `OMADA_MAX_SSIDS` is treated as a manual upper
bound; when `iw list` reports no valid multi-interface combinations, the agent
caps the advertised and applied SSID count to one AP interface so hostapd does
not fail the whole radio.

## By message family

This table tracks **implementation status**, not merely known numeric message
identifiers.

| Family | Status | Notes |
| --- | --- | --- |
| Discovery | ✅ | Site-scoped UDP discovery implemented |
| Pre-adopt | ✅ | `adoptPort` honored |
| Pre-connect | ✅ | First adoption and `rebuild=1` managed reconnect |
| Device verification | ✅ | Legacy V2 `cipherType=5` branch |
| System verification | ✅ | Mutual auth checked before success |
| Device negotiation | ✅ | Conservative AP component map. `devCap` (mimo, supportRadioMode, supportOFDMA), `radioCap` (mimo, ofdma, mcsLevel, `maxPow` = highest usable channel `mPow` from `iw phy`; the controller's Tx Power range and High/Auto use it) and `channelInfo` (channels, width/band/DFS flags, max power) are built from `iw list` and `iw reg get`; values follow real EAPs of the same Wi-Fi generation (Observed: EAP650 adopt data; Validated: lab adoption as EAP683 UR on Controller 6.3.0.111) |
| System negotiation | ✅ | Config/version metadata consumed |
| Initial sync result | ✅ | ACK validated |
| Inform | 🟡 | `upTime` as `"<days> days HH:MM:SS"` and `lanInfo.rate` as an integer string (Validated: lab, no controller errors); clients without `rssi` are left out until hostapd reports one. Device, wired LAN, active hostapd clients enriched with DHCP/openNDS metadata, portal auth clients, hostapd station metrics, OpenWrt `wSettings_*`, `ssidStats_*`, and aggregate `radioTraffic_*` when `ubus` is available |
| SET | 🟡 | Envelope/version ACKs plus parsed AP domains; one-off commands without a version (`rogueApScan`) keep the current version; unsupported keys return local error instead of fake success; common controller defaults such as `ssh`, `lldp`, `ipGroup`, `ipv6Group`, `lanSetting`, `logSetting`, and scheduler/mac-filter globals are accepted as passive when they do not require OpenWrt changes |
| WLAN/radio config | 🟡 | OpenWrt UCI adapter for basic radios and PSK SSIDs. `chanWidth` enum codes map to `htmode` (Observed: 2=20, 3=40, 4=20/40, 5=80, 6=20/40/80, 7=160 MHz); Auto channel uses ACS limited to `channelRange` and usable channels; `OMADA_WIFI_COUNTRY` sets the country (the controller sends none); `wirelessMode` codes (3 b/g, 4 b/g/n, 13 b/g/n/ax, 7 a/n, 10 a/n/ac, 16 a/n/ac/ax; Auto = key absent) select the htmode family, capped at the hardware (Validated: lab, b/g → `NOHT`/`hw_mode=g`, a/n/ac → `VHT80`, Auto → `HE`) |
| SSID VLAN config | 🟡 | Opt-in UCI network interface mapping; dynamic VLAN/Option82 remain rejected unless implemented |
| Management VLAN config | 🟡 | Opt-in UCI device remapping via explicit local target variables |
| Client operations | 🟡 | Reconnect/deauth through hostapd `ubus`; block/unblock and rate-limits through nftables bridge policy |
| LED config | 🟡 | Optional sysfs brightness adapter for `led.enable`; optional trigger adapter for `led.locate`, written in the order Linux LED rules need so the LED keeps its On/Off state after Locate (Validated: lab, Controller 6.3.0.111); `wifiControlLed` remains rejected |
| FORGET | ✅ | `FORGET_REQUEST` and no-reset variant are ACKed and clear local reconnect state |
| GET | 🟡 | Handler replies with correlated `GET_RESPONSE` and unsupported-key metadata |
| NOTIFY | 🟡 | Handler replies to v1/v2 notify requests unless `nre=1`; payload-specific handlers are not implemented |
| REPORT | 🚧 | ECSP identifier and `Report.reportData` envelope are known. AP client report protobuf DTOs were identified (`MonitorMessageDTO`, `ComponentDTO`, `ApClientReportDTO`, `WirelessClientDTO`), but the exact `reportData` string serializer used by device-side REPORT is not implemented yet. |
| Upgrade | 🚧 | Not implemented |
| File transfer | 🚧 | Not implemented |
| Remote terminal | 🚧 | Not implemented |
| Radio telemetry | 🟡 | `wSettings_*` with the operating `ch`/`bw` from `iw dev <if> info`, `rdMode` (e.g. `a/n/ac/ax mixed`), `txPower` as `"<n>dBm"`, and `busyUti`/`txUti`/`rxUti`/`interUti` from `iw survey dump` between informs (Validated: lab UI shows mode/channel/width); aggregate `radioTraffic_*` from hostapd bytes/packet counters where available |
| SSID telemetry | 🟡 | `ssidStats_*.id` is the controller SSID id (uci `openomada_ssid_id`), `-1` for SSIDs it does not manage (Validated: lab); OpenWrt SSID/client counts, BSSID in Omada MAC format, optional interface counters, and hostapd traffic counters |
| Client telemetry | 🟡 | Active hostapd station RSSI/SNR/rate/traffic enriched with DHCP IP/hostname and openNDS portal state; stale DHCP/openNDS-only clients are filtered out |
| Captive portal | 🟡 | openNDS enforcement, free-policy walled garden, TP-Link EAP External Portal query redirects, `clientConfig.unauth`, and client-state reporting are available on OpenWrt; hosted portal UI and Controller `/hotspot/extPortal/auth` submission are external to the agent |
| Radio advanced | 🟡 | `loadBalance_*` → UCI `maxassoc` (hostapd `iface_max_num_sta`), `rssi_*` → `rssi_reject_assoc_rssi`, `qosConfig_*.uapsdEnable` → `uapsd` on agent-managed SSIDs; disabled limits remove the option. Validated: lab, values reach `/var/run/hostapd-phy0.conf` and are removed again. Other `qosConfig_*` WMM/EDCA fields and `wirelessAdv_*` (OFDMA, beacon/DTIM/RTS) are accepted without local changes |
| Network Check | 🟡 | Components `ping`/`traceroute`/`dnslookup`/`arptable` (`1.0`) show Tools → Network Check. SET keys carry `cmdId`, `dest`, `additional{cnt, pktSize}`. Output goes back as plain `NOTIFY_REQUEST` (type 80; `NOTIFY_REQUEST_V2` from an AP is rejected with "Not supported device type: ap") with `sub=5` (TROUBLESHOOTING), `nre=1`, `ctnt={cmdId, troubleshooting{msg, bof, seq, finish}}`; `msg` must be base64 because the UI decodes it with `atob` (Validated: lab, 6.3.0.111) |
| SNMP | 🟡 | Component `snmp` (`1.0`, advertised when `snmpd` is installed) shows Config → Services → SNMP. SET key `snmp` carries `location`, `contact`, `v1v2cEnable`, `community`, `v3Enable`, `v3Username`, `v3SecurityLevel` (0 noAuthNoPriv, 1 authNoPriv, 2 authPriv), `v3AuthMode` (1 MD5, 2 SHA), `v3Password`, `v3PrivacyMode` (1 DES, 2 AES), `v3PrivacyPassword`. The v3 level/mode fields and privacy password are sent only when the AP lists component `service` >= `1.1` (`ApImage.supportSnmpV3Plus`); otherwise just `v3Username`/`v3Password` arrive, so the agent advertises `service` `1.1` with `snmp`. Applied to `/etc/config/snmpd` (read-only access); snmpd is stopped when both versions are off (Validated: lab, 6.3.0.111, `snmpget` v2c and v3 authPriv SHA/AES)
| LLDP | 🟡 | `devCap.supportLldp=1` with components `lldp` and `lldpInform` (`1.0`, advertised when `lldpd` and `lldpcli` are installed) shows the per-AP LLDP switch (Observed: 6.3.0.111 model templates list all three for EAPs). SET key `lldp` is `{"enable": 1|0}`; the controller resolves the per-AP and site settings itself. On: `lldpd.config` gets `enable_lldp=1`, CDP/FDP/SONMP/EDP off, `interface` = the LAN bridge's wired ports, and the package's placeholder LLDP-MED `lldp_location` is removed; lldpd is enabled and restarted. The chassis ID is a LAN port's MAC (subtype MAC), so on devices whose ports have random MACs it is not the device MAC. Off: lldpd is stopped and disabled. While lldpd runs, inform key `lldp` carries `{"lldps": [{"port": <OMADA_LAN_PORT>, "neighbors": [{"chassisId", "portId", "name", "description", "capabilities"}]}]}` from `lldpcli -f json0 show neighbors` (cached 30 s). `capabilities` is 8 characters of `0`/`1` for station, DOCSIS, telephone, router, WLAN, bridge, repeater, other; a MAC chassis ID is sent as `AA-BB-CC-DD-EE-FF` (Inferred: matches how the controller writes device MACs) |
| Site time zone | 🟡 | SYSTEM_NEGOTIATION carries `timeSetting` (`timeZone`, `date`, `time`, `ntpEnable`) and `dst` (`enable`, `start`, `end`, `offset`) at every connect. `timeZone` is the site's standard UTC offset in minutes plus 720 (Observed: 1050 for UTC+05:30; the zone name is not sent). The agent writes the offset as a POSIX TZ string to `system.@system[0].timezone` (e.g. `<+0530>-5:30`), removes `zonename`, and reloads `/etc/init.d/system`; nothing is written when it already matches. `dst` carries `enable`, `offset` (seconds) and the next `start`/`end` as Unix times (Observed: London site → last Sundays of March and October 2027, 01:00 UTC); they become POSIX `Mm.w.d/time` rules, e.g. `<+00>0<+01>-1,M3.5.0/1,M10.5.0`. A transition on one of the last 7 days of a month is taken as "last" (Inferred: the UI offers First to Fourth and Last). Rules that are not "Nth weekday of the month" (some years in Chile or Israel) are approximated until the controller sends the next period. Out-of-range `dst` values are ignored (standard offset only); a SET without `dst` keeps the daylight-saving rules already on the AP. Saving the site's time settings sends a live SET with `timeSetting` and `dst`. NTP and the clock are left alone. `OMADA_CAP_TIMEZONE=false` turns it off (Validated: lab, 6.3.0.111, India → `<+0530>-5:30`, London with Auto DST → rules above; London/Sydney/New York rules match tzdata for 2026–2029) |
| IP Settings | 🟡 Opt-in | Device Config → IP Settings sends SET key `lanSetting` (`connType` 0 static / 1 DHCP, `ipAddr`, `netMask`, `gateway`, `preferredDNS`, `alternateDNS`, `useFallBack`, `fallBackIp`, `fallBackMask`, `fallbackGateway`). With `OMADA_CAP_IP_SETTINGS=true` it is applied to `network.<lan>` 2 s after SET_RESPONSE. A detached watchdog restores the whole previous `/etc/config/network` (so other network edits made in that window are reverted too) after 180 s unless the agent opens a TCP connection to the controller from the new address. The ECSP session is restarted so it binds to the new address. After a rollback the controller still holds the bad setting and resends it on a full sync, so the agent skips a change it rolled back (until reboot). DEVICE_NEGOTIATION reports the running `lanSetting`. DHCP fallback: the fallback address is stored as `network.<lan>.openomada_fallback` (+ `_gateway`) and reported back; while DHCP has no lease for 30 s the agent adds it to the LAN device with `ip` (plus a default route through the fallback gateway if none exists) and removes it once a lease arrives. The 30 s delay is our choice (Inferred: real EAP timing unknown). If `ip addr add` fails, nothing is recorded and the agent tries again after another 30 s (Inferred: unit tests only). Changing only the fallback address swaps it at once (Inferred: unit tests only); writing the fallback options does not restart the LAN interface (Validated: lab AP, OpenWrt 25.12, interface uptime kept counting). (Validated: lab, 6.3.0.111, static .2→.3→.2, unreachable subnet rollback, resend after rollback skipped) |
| SSH | ⏸️ Not applied by design | Site Network Config → SSH sends SET key `ssh` (`sshenable`, `sshserverPort`, `layer3Access`). The agent accepts it without changing dropbear: the controller default is SSH off, which would lock users out of OpenWrt's main admin path on the first config push. Open an issue if you need the controller to manage SSH |
| Rogue AP / interference scan | 🟡 | `rogueApScan` runs `iw dev <ap> scan ap-force` per band in the background; the next inform carries `rogueApList` (`mac`, `ssid`, `rid`, `ch`, `sec`, `bcn`, `signal`, `pm`, using the controller's security and phy-mode codes; own BSSIDs left out). Sent once: a repeated list makes the controller log `monitorScanStatusImageEnum is null`. Validated: lab, Statistics → Interference Detection lists both bands. `rogueApGlobal`/`rogueApSetting` are passive; no periodic background scan, no `bandWidth` |
| Mesh | 🚧 | Not implemented |
| Switch profile | 🚧 | Not implemented |
| Gateway profile | 🚧 | Not implemented |
| OLT profile | 🚧 | Not implemented |

## Parked work

Known gaps that are deliberately not being worked on now, and why.

| Item | Why it is parked | What would unblock it |
| --- | --- | --- |
| Guest logout (portal logout domain) | The controller sends `controllerSetting.logoutDomain` (acknowledged, not applied). What a real EAP does when a guest opens it (redirect, record removal, `EVENT_PORTAL_AUTH`) is Unknown. | Observe it on a real EAP, then map it to openNDS deauthentication (est. 2 h). |
| Switch and other device profiles | The agent reports itself as an AP in several places (`device: "ap"`). A device-neutral refactor is the groundwork for switch profiles; it changes nothing visible for an AP. | A concrete switch target to test against (est. one day for the refactor). |
| 160 MHz and DFS under country IN | wireless-regdb gives India `DFS-UNSET`, so hostapd cannot do radar detection and disables the radio; DFS channels and 160 MHz stay hidden (Validated: lab AP, OpenWrt 25.12). Patching the local regulatory database was declined. | A DFS region for IN in upstream wireless-regdb. |
| Periodic rogue AP scan | Controller 6.3.0.111 stores an unsolicited `rogueApList` as rogue APs only on cloud-based controllers; software and hardware controllers ignore it (Observed). Scanning also costs off-channel time. | A local controller that shows the results. |

## Capability policy

A protocol component should not be added to the advertised component map merely
because its name/version is known. It should be advertised only after the agent
can safely process the controller behavior that enabling the component causes.

## MAC serialization policy

The agent keeps MAC addresses internally in normalized lower-case colon form for
comparison and merging. Any MAC emitted toward Omada is converted at the
boundary to `AA-BB-CC-DD-EE-FF`; this includes ECSP headers, `mainMac`, client
stats, SSID BSSID values, RADIUS Calling/Called-Station-Id values, and openNDS
External Portal redirect parameters.
