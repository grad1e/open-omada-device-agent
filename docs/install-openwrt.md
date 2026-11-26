# Install on an OpenWrt access point

This guide takes a freshly reset OpenWrt device to an access point adopted by
your Omada controller. It was written and tested on a JioRouter AX6000
(JIDU6101) with OpenWrt 25.12.6; see [JioRouter AX6000 notes](#jiorouter-ax6000-jidu6101-notes)
for what is specific to it.

You need:

- An access point running **OpenWrt 23.05 or newer** (tested on 25.12) with
  about 30 MB of free flash for Python.
- A computer with `git`, `bash` and `ssh` on the same network.
- An Omada controller the access point can reach (UDP 29810 and TCP 29814).

## 1. Make OpenWrt an access point on your network

Skip this step if the device already works as an access point on your LAN.

A freshly reset OpenWrt device is a router at `192.168.1.1` with its own DHCP
server. Keep it off your network until this step is done. Connect your
computer straight to one of its LAN ports, then log in:

```bash
ssh root@192.168.1.1
```

Set a root password (`passwd`), then give the LAN an address on your network
and turn off the device's own DHCP server. Replace the addresses with your
network's:

```sh
uci set network.lan.proto='static'
uci set network.lan.ipaddr='192.168.10.2/24'
uci set network.lan.gateway='192.168.10.1'
uci add_list network.lan.dns='192.168.10.1'
uci set dhcp.lan.ignore='1'
uci set dhcp.lan.dhcpv6='disabled'
uci set dhcp.lan.ra='disabled'
uci commit
reboot
```

For an address from your router instead (for example an Omada gateway's
"Use Fixed IP Address" reservation), use `proto='dhcp'` and leave out
`ipaddr`, `gateway` and `dns`. Some devices make up a new `br-lan` MAC at
every boot, so a reservation would never match; the agent log tells you if
yours does and which `macaddr` line to add to `/etc/config/network`.

Plug the access point into your network with a LAN port and check that it
answers at its new address: `ssh root@192.168.10.2`.

## 2. Write the agent settings

On your computer:

```bash
git clone https://github.com/grad1e/open-omada-device-agent.git
cd open-omada-device-agent
```

Create a file named `.env.ap` (it holds a password; Git ignores
`.env.*` files):

```dotenv
OMADA_CONTROLLER_HOST=192.168.10.10
OMADA_PLATFORM=openwrt

# Device Account. A new access point is adopted with admin / admin.
OMADA_DEVICE_USERNAME=admin
OMADA_DEVICE_PASSWORD=admin

# What the controller shows. Use a dual-band model for a dual-band AP.
OMADA_DEVICE_NAME=OpenWrt-AP
OMADA_DEVICE_MODEL=EAP683 UR
OMADA_DEVICE_MODEL_VERSION=1.0
OMADA_DEVICE_HARDWARE_VERSION=1.0
OMADA_DEVICE_FIRMWARE_VERSION=1.0.13
OMADA_RADIO_BANDS=2g,5g

# Two-letter Wi-Fi country code, e.g. US, DE, IN.
OMADA_WIFI_COUNTRY=US
```

Optional settings are listed in [docs/configuration.md](configuration.md).
Useful ones:

- `OMADA_CAP_IP_SETTINGS=true`: let the controller change the AP's IP
  settings (Device Config → IP Settings). Changes roll back after 3 minutes if
  the AP cannot reach the controller from the new address.
- `OMADA_LED_BRIGHTNESS_PATH` / `OMADA_LED_TRIGGER_PATH`: the LED used for
  Locate, e.g. `/sys/class/leds/<name>/brightness` (`ls /sys/class/leds`).

## 3. Install

```bash
scripts/deploy-openwrt.sh --host root@192.168.10.2 --env .env.ap
```

SSH asks for the AP's root password once. If you reset this AP before, first
remove its old SSH key with `ssh-keygen -R 192.168.10.2`.

The script installs `python3`, `python3-dotenv` and `python3-psutil` (plus
`snmpd`, `opennds`, `dnsmasq-full` and `conntrack` for SNMP and the captive
portal), copies the agent to `/root/open-omada-device-agent`, copies `.env.ap`
there as `.env`, installs the `open-omada-agent` boot service and starts it.
The AP needs internet access for the packages. openNDS stays off until the
controller configures a portal.

## 4. Adopt it in the controller

The AP appears in the controller under **Devices** as **Pending** within a
few seconds. Click **Adopt**. If the controller asks for a username and
password, enter `admin` / `admin`.

After it shows **Connected**, the controller has given the AP your site's
Device Account (Site Settings → Device Account). Put that password in `.env.ap`
as `OMADA_DEVICE_PASSWORD` and push it, so reconnects keep working:

```bash
scripts/deploy-openwrt.sh --host root@192.168.10.2 --env .env.ap --no-packages
```

If you adopt the AP again later (after forgetting it, or after its MAC
changed), the controller first tries `admin` / `admin` and shows **Adopt
Failed**. Click **Adopt** again and enter the site Device Account.

## Check and update

On the AP:

```sh
logread -e open_omada | tail -n 30      # agent log
/etc/init.d/open-omada-agent restart    # restart the agent
```

To update, pull the latest code and run the script again without `--env`
(the AP keeps its `.env`):

```bash
git pull
scripts/deploy-openwrt.sh --host root@192.168.10.2
```

To remove the agent, run on the AP:

```sh
/etc/init.d/open-omada-agent disable
/etc/init.d/open-omada-agent stop
rm -rf /etc/init.d/open-omada-agent /usr/bin/open-omada-agent /root/open-omada-device-agent
```

Then forget the device in the controller.

## The deploy script

`scripts/deploy-openwrt.sh` copies the current source to an OpenWrt access
point and restarts the agent:

```bash
scripts/deploy-openwrt.sh --host root@192.168.10.2
```

It installs the packages the agent uses (`python3`, `python3-dotenv`,
`python3-psutil`, `snmpd`, `opennds`, `dnsmasq-full`, `conntrack`) with `apk`
or `opkg`; pass `--no-packages` to skip that. It syncs the source tree with
`tar` over SSH and keeps the AP's `.env` unless you pass `--env FILE`. It
writes the `/usr/bin/open-omada-agent` launcher, installs and enables the
`open-omada-agent` boot service if it is missing, restarts it, and prints the
last lines of `logread -e open_omada`. Run `scripts/deploy-openwrt.sh --help`
for all options (`--remote-dir`, `--no-restart`, `--no-packages`).

## JioRouter AX6000 (JIDU6101) notes

- **Random MAC on some units.** Units with the December 2024 bootloader
  (`MTKJIO_ATF_U-Boot_6148e527`) have no MAC address in flash that OpenWrt
  can read, so the Ethernet MAC changes at every boot
  ([openwrt/openwrt#26967](https://github.com/openwrt/openwrt/issues/26967)).
  The agent warns about it. Pin the MAC from the label on the router before
  you adopt it:

  ```sh
  uci set network.@device[0].macaddr='<label MAC>'   # the br-lan device
  uci commit network
  reboot
  ```

  If the AP was already adopted with the random MAC, delete
  `/root/open-omada-device-agent/.omada-device-mac`, forget the AP in the
  controller, and adopt it again after the reboot.
- **Locate LED:** `OMADA_LED_BRIGHTNESS_PATH=/sys/class/leds/red:status/brightness`
  and `OMADA_LED_TRIGGER_PATH=/sys/class/leds/red:status/trigger`.
- **Model to advertise:** `EAP683 UR` (dual-band Wi-Fi 6, 4x4, 160 MHz), as in
  the example settings above. Set `OMADA_WIFI_COUNTRY=IN` in India.
