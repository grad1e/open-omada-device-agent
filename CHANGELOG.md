# Changelog

All notable project changes will be documented in this file.

## Unreleased

### Added

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

- `OMADA_DEVICE_IP` defaults to `local`: the agent advertises the address it
  uses to reach the controller (a UDP route lookup that sends no packet),
  re-evaluated every inform. Previously `0.0.0.0` was shown unless the address
  was set by hand. Fixed addresses and `auto` (public lookup) still work.
- Unsupported AP `SET_REQUEST` keys now return a local config error instead of
  being silently acknowledged.

### Fixed

- `wSettings_*.txPower` is reported as `"<n>dBm"`; a bare number made
  Controller 6.2/6.3 drop every inform and show the AP as "Heartbeat Missed".
- Omada `chanWidth` enum codes (2=20, 3=40, 5=80, 6=160 MHz) are mapped to
  OpenWrt `htmode`, keeping the radio's HE/VHT/HT family instead of forcing HT20.
- Channel capability flags include the base 20 MHz bit, so the controller
  offers channels for a 20 MHz width.
- `led.enable` no longer fails the whole SET_REQUEST when LED control is not
  configured; it is treated as passive.

### Documentation

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
