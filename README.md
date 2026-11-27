# Open Omada Device Agent

Run your own OpenWrt access point under a TP-Link **Omada controller**, as if
it were an Omada EAP. Adopt it, set its Wi-Fi and see its clients from the
controller, next to your real Omada gear.

> [!WARNING]
> Unofficial, alpha software. Not affiliated with or endorsed by TP-Link. Try
> it on a lab or home network first.

## Built for the JioRouter AX6000 (JIDU6101)

This project is developed and tested on one device:

| | |
| --- | --- |
| Hardware | JioRouter AX6000 (JIDU6101), dual-band Wi-Fi 6 |
| Firmware | OpenWrt 25.12.6 |
| Controller | Omada Network Application 6.3.0.111 |
| Shows up as | EAP683 UR |

The agent only uses standard OpenWrt tools (`uci`, `hostapd`, `iw`, `ubus`),
so other OpenWrt access points will probably work too, but nobody has
checked them yet. **If you run it on another device, please open a pull
request** with what you changed and what you tested: model, OpenWrt version
and controller version. Fixes and device notes for other hardware are very
welcome. See [CONTRIBUTING.md](CONTRIBUTING.md).

## What works

- **Adoption:** the AP appears as Pending, adopts with one click, and
  reconnects by itself after a restart or a lost connection.
- **Wi-Fi:** SSIDs, channel (including Auto), width up to 160 MHz, transmit
  power, wireless mode, country, client limit and minimum RSSI.
- **SSID options:** WPA2/WPA3 security, PMF, 802.11r, MAC filtering, WLAN
  schedules and 802.11 rate control.
- **Monitoring:** clients with IP and name, traffic, live channel and channel
  use per radio.
- **Tools:** Locate (blinks an LED), Network Check (ping, traceroute, DNS,
  ARP) and the interference scan.
- **Optional:** IP Settings, SNMP, LLDP, SSID and management VLANs, and a captive
  portal on a separate guest network.

Not yet: guest logout from the portal, band steering, Prohibit Wi-Fi
Sharing, conditional SSID broadcast, firmware upgrade, mesh, and switch or
gateway devices. The full list is in
[docs/protocol-status.md](docs/protocol-status.md).

## Install

You need an OpenWrt access point, a computer with `git`, `bash` and `ssh`,
and an Omada controller it can reach. In short:

1. Turn the OpenWrt device into an access point on your network.
2. Clone this repository and write a small settings file, `.env.ap`.
3. Run `scripts/deploy-openwrt.sh --host root@<AP address> --env .env.ap`.
4. Click **Adopt** in the controller.

Follow the full guide, with every command:
**[Install on an OpenWrt access point](docs/install-openwrt.md)**.

### Set it up with an AI agent

If you use an AI coding agent that can run commands on your computer (Claude
Code, Codex, Gemini CLI or similar), start it in an empty folder and paste
this prompt. It reads the guide, asks you for your network details, and runs
the steps with you. Passwords stay with you: it asks you to type them.

```text
Help me install Open Omada Device Agent on an OpenWrt access point so my
TP-Link Omada controller can adopt it.

1. Clone https://github.com/grad1e/open-omada-device-agent and read
   README.md, docs/install-openwrt.md and AGENTS.md. Follow the install
   guide step by step; do not improvise other methods.
2. Before doing anything, ask me for: the AP model and how I can reach it
   now (its IP, or that it is freshly reset at 192.168.1.1), the IP address
   it should have on my network, my gateway, the Omada controller's IP, and
   my two-letter Wi-Fi country code. If the AP is a JioRouter AX6000
   (JIDU6101), also ask for the MAC address on its label and follow the
   JIDU6101 notes in the guide.
3. Show me each command before you run it, and wait for my OK before
   anything that changes the AP's network, reboots it or resets it.
4. Never ask me to paste a password into the chat, and never put one in a
   command line. When a password is needed (SSH, the AP's root password, the
   controller's Device Account), tell me where to type it myself, for
   example in the .env.ap file or at the ssh prompt.
5. Do not change my Omada controller's settings. When it is time to adopt,
   tell me what to click.
6. When the AP shows "Connected" in the controller, help me switch
   OMADA_DEVICE_PASSWORD to my site's Device Account password, as the guide
   describes. Then show me how to read the agent log and how to update later.

If a step fails, show me the error and the relevant log lines, and suggest
a fix before trying anything else.
```

## How it works

Omada devices talk to the controller over TP-Link's ECSP protocol: UDP for
discovery, then TLS for adoption, configuration and status reports. This
agent implements the access-point side of that protocol from observed
behavior, and turns the controller's settings into OpenWrt configuration.

## Documentation

| Read this | For |
| --- | --- |
| [Install on OpenWrt](docs/install-openwrt.md) | Step-by-step setup, updates, JIDU6101 notes |
| [Configuration](docs/configuration.md) | Every setting in `.env` |
| [Captive portal](docs/captive-portal.md) | Portal SSIDs with openNDS |
| [Feature status](docs/protocol-status.md) | What is implemented, in detail |
| [Development](docs/development.md) | Running tests and the agent from source |
| [Protocol](docs/protocol-overview.md), [lifecycle](docs/device-lifecycle.md), [architecture](docs/architecture.md) | How ECSP and the code fit together |

## Contributing

Pull requests go to the `develop` branch. Read [CONTRIBUTING.md](CONTRIBUTING.md)
first; it covers device support, testing on hardware and AI-assisted
changes. Report security problems privately as described in
[SECURITY.md](SECURITY.md).

## Credits and license

This is an independently developed fork of
[adryanalencar/open-omada-device-agent](https://github.com/adryanalencar/open-omada-device-agent).
It contains no TP-Link firmware, source code, keys or other proprietary
material. Apache License 2.0; see [LICENSE](LICENSE) and [NOTICE](NOTICE).
