# Protocol Support Matrix

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
| SET | 🟡 | Envelope/version ACKs plus parsed AP domains; one-off commands without a version (`rogueApScan`) keep the current version; unsupported keys return local error instead of fake success; common controller defaults such as `ssh`, `snmp`, `lldp`, `ipGroup`, `ipv6Group`, `lanSetting`, `logSetting`, and scheduler/mac-filter globals are accepted as passive when they do not require OpenWrt changes |
| WLAN/radio config | 🟡 | OpenWrt UCI adapter for basic radios and PSK SSIDs. `chanWidth` enum codes map to `htmode` (Observed: 2=20, 3=40, 4=20/40, 5=80, 6=20/40/80, 7=160 MHz); Auto channel uses ACS limited to `channelRange` and usable channels; `OMADA_WIFI_COUNTRY` sets the country (the controller sends none) |
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
| Rogue AP / interference scan | 🟡 | `rogueApScan` runs `iw dev <ap> scan ap-force` per band in the background; the next inform carries `rogueApList` (`mac`, `ssid`, `rid`, `ch`, `sec`, `bcn`, `signal`, `pm`, using the controller's security and phy-mode codes; own BSSIDs left out). Sent once: a repeated list makes the controller log `monitorScanStatusImageEnum is null`. Validated: lab, Statistics → Interference Detection lists both bands. `rogueApGlobal`/`rogueApSetting` are passive; no periodic background scan, no `bandWidth` |
| Mesh | 🚧 | Not implemented |
| Switch profile | 🚧 | Not implemented |
| Gateway profile | 🚧 | Not implemented |
| OLT profile | 🚧 | Not implemented |

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
