#!/usr/bin/env bash
set -euo pipefail

OPENWRT_HOST="${OPENWRT_HOST:-}"
REMOTE_DIR="${REMOTE_DIR:-/root/open-omada-device-agent}"
ENV_FILE="${ENV_FILE:-}"
REMOTE_PYTHON="${REMOTE_PYTHON:-/usr/bin/python3}"
REMOTE_AGENT="${REMOTE_AGENT:-/usr/bin/open-omada-agent}"
AGENT_ARGS="${AGENT_ARGS:---debug}"
LOG_FILE="${LOG_FILE:-/tmp/open-omada-agent.log}"
# One shared SSH connection, so a password is asked for once. A new AP's host
# key is saved to known_hosts; after a factory reset remove the old one with
# ssh-keygen -R <host>.
SSH_OPTS="${SSH_OPTS:--o StrictHostKeyChecking=accept-new -o ControlMaster=auto -o ControlPath=/tmp/open-omada-ssh-%C -o ControlPersist=60}"
RESTART=1
INSTALL_PACKAGES="${INSTALL_PACKAGES:-1}"

usage() {
    cat <<'EOF'
Usage: scripts/deploy-openwrt.sh --host USER@HOST [options]

Installs or updates the agent on an OpenWrt access point over SSH: installs
Python and the optional packages, copies the source, creates the
open-omada-agent launcher, installs the boot service, then (re)starts it.

Options:
  --host USER@HOST      SSH target, e.g. root@192.168.1.2 (required)
  --env FILE            Copy this local file to the AP as .env (first install,
                        or to replace the AP's settings)
  --remote-dir PATH     Remote install path. Default: /root/open-omada-device-agent
  --agent-args ARGS     Arguments passed to open-omada-agent. Default: --debug
  --log-file PATH       Remote log file. Default: /tmp/open-omada-agent.log
  --no-restart          Copy files but do not restart the agent
  --no-packages         Do not install OpenWrt packages (python3,
                        python3-dotenv, python3-psutil, snmpd, lldpd,
                        opennds, dnsmasq-full, conntrack) with apk or opkg
  -h, --help            Show this help

Environment overrides:
  OPENWRT_HOST, ENV_FILE, REMOTE_DIR, REMOTE_PYTHON, REMOTE_AGENT, AGENT_ARGS, LOG_FILE, SSH_OPTS,
  INSTALL_PACKAGES (0 skips the package step, like --no-packages)
EOF
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --host)
            OPENWRT_HOST="${2:?missing value for --host}"
            shift 2
            ;;
        --env)
            ENV_FILE="${2:?missing value for --env}"
            shift 2
            ;;
        --remote-dir)
            REMOTE_DIR="${2:?missing value for --remote-dir}"
            shift 2
            ;;
        --agent-args)
            AGENT_ARGS="${2:?missing value for --agent-args}"
            shift 2
            ;;
        --log-file)
            LOG_FILE="${2:?missing value for --log-file}"
            shift 2
            ;;
        --no-restart)
            RESTART=0
            shift
            ;;
        --no-packages)
            INSTALL_PACKAGES=0
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            echo "Unknown option: $1" >&2
            usage >&2
            exit 2
            ;;
    esac
done

if [ -z "$OPENWRT_HOST" ]; then
    echo "Missing --host USER@HOST (the access point's SSH address)" >&2
    usage >&2
    exit 2
fi
if [ -n "$ENV_FILE" ] && [ ! -f "$ENV_FILE" ]; then
    echo "No such file: $ENV_FILE" >&2
    exit 2
fi

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [ ! -d "$PROJECT_ROOT/src/open_omada_device_agent" ]; then
    echo "Could not find src/open_omada_device_agent under $PROJECT_ROOT" >&2
    exit 1
fi

echo "Deploying Open Omada agent source to $OPENWRT_HOST:$REMOTE_DIR"

# shellcheck disable=SC2086
COPYFILE_DISABLE=1 tar --no-xattrs -C "$PROJECT_ROOT" -cf - src/open_omada_device_agent agent.py pyproject.toml README.md docs \
    .env.example scripts/openwrt/open-omada-agent.init \
    | ssh $SSH_OPTS "$OPENWRT_HOST" "mkdir -p '$REMOTE_DIR' && tar -C '$REMOTE_DIR' -xf -"

if [ "$INSTALL_PACKAGES" -ne 0 ]; then
    echo "Installing OpenWrt packages (skip with --no-packages)"
    # shellcheck disable=SC2086
    if ! ssh $SSH_OPTS "$OPENWRT_HOST" /bin/ash -s <<'PACKAGES'
set -eu

# python3, python3-dotenv, python3-psutil: the agent itself.
# snmpd: Device Config -> Services -> SNMP. It stays stopped until the
# controller enables SNMP; the agent then starts it.
# lldpd: Device Config -> Services -> LLDP, likewise started by the agent.
# opennds, dnsmasq-full, conntrack: captive portal.
if command -v apk >/dev/null 2>&1; then
    PM=apk
    SNMPD=snmpd-ssl   # the SSL build supports SNMPv3 privacy (DES/AES)
elif command -v opkg >/dev/null 2>&1; then
    PM=opkg
    SNMPD=snmpd
else
    echo "Warning: neither apk nor opkg found; install python3, python3-dotenv, python3-psutil, snmpd, lldpd, opennds, dnsmasq-full and conntrack by hand" >&2
    exit 0
fi

pm_installed() {
    if [ "$PM" = apk ]; then
        apk info -e "$1" >/dev/null 2>&1
    else
        [ -n "$(opkg list-installed "$1" 2>/dev/null)" ]
    fi
}

had_snmpd=0
[ -x /usr/sbin/snmpd ] && had_snmpd=1
had_lldpd=0
[ -x /usr/sbin/lldpd ] && had_lldpd=1

if [ "$PM" = apk ]; then
    apk update >/dev/null
else
    opkg update >/dev/null
fi

missing=""
for pkg in python3 python3-dotenv python3-psutil "$SNMPD" lldpd conntrack; do
    pm_installed "$pkg" || missing="$missing $pkg"
done
# A package that provides /usr/sbin/snmpd (e.g. snmpd-nossl) is enough.
if [ "$had_snmpd" -eq 1 ]; then
    missing=$(echo "$missing" | sed "s/ $SNMPD//")
fi
if [ -n "$missing" ]; then
    echo "Installing:$missing"
    if [ "$PM" = apk ]; then
        # shellcheck disable=SC2086
        apk add $missing
    else
        # shellcheck disable=SC2086
        opkg install $missing
    fi
fi

# dnsmasq-full replaces dnsmasq (both ship /usr/sbin/dnsmasq). apk swaps them
# in one transaction after downloading. opkg cannot, and removing dnsmasq stops
# local DNS, so download the package before removing dnsmasq.
if ! pm_installed dnsmasq-full; then
    echo "Replacing dnsmasq with dnsmasq-full"
    dir=/tmp/open-omada-packages
    rm -rf "$dir"
    mkdir -p "$dir"
    cp /etc/config/dhcp "$dir/dhcp.config" 2>/dev/null || true
    if [ "$PM" = apk ]; then
        apk add dnsmasq-full
    else
        (cd "$dir" && opkg download dnsmasq-full >/dev/null)
        pkgfile=$(ls "$dir"/dnsmasq-full*.ipk 2>/dev/null | head -n 1)
        if [ -z "$pkgfile" ]; then
            echo "Warning: could not download dnsmasq-full; keeping dnsmasq" >&2
        else
            pm_installed dnsmasq && opkg remove dnsmasq
            opkg install "$pkgfile"
        fi
    fi
    if [ ! -f /etc/config/dhcp ] && [ -f "$dir/dhcp.config" ]; then
        cp "$dir/dhcp.config" /etc/config/dhcp
    fi
    /etc/init.d/dnsmasq restart >/dev/null 2>&1 || true
    rm -rf "$dir"
fi

# openNDS starts on br-lan as soon as it is installed, which cuts the AP's
# own traffic (and any later download). Install it last and switch it off;
# the agent turns it on when the controller configures a portal.
if ! pm_installed opennds; then
    echo "Installing: opennds"
    if [ "$PM" = apk ]; then
        apk add opennds
    else
        opkg install opennds
    fi
    uci -q set opennds.@opennds[0].enabled='0' && uci commit opennds
    /etc/init.d/opennds stop >/dev/null 2>&1 || true
    echo "opennds installed; it starts when the controller configures a portal"
fi

if [ "$had_snmpd" -eq 0 ] && [ -x /etc/init.d/snmpd ]; then
    /etc/init.d/snmpd stop >/dev/null 2>&1 || true
    /etc/init.d/snmpd disable >/dev/null 2>&1 || true
    echo "snmpd installed; it starts when SNMP is enabled in the controller"
fi
if [ "$had_lldpd" -eq 0 ] && [ -x /etc/init.d/lldpd ]; then
    /etc/init.d/lldpd stop >/dev/null 2>&1 || true
    /etc/init.d/lldpd disable >/dev/null 2>&1 || true
    echo "lldpd installed; it starts when LLDP is enabled in the controller"
fi
echo "Packages ready: python3 python3-dotenv python3-psutil $SNMPD lldpd opennds dnsmasq-full conntrack"
PACKAGES
    then
        echo "Warning: package step failed (no internet on the AP?); deploying anyway" >&2
    fi
fi

if [ -n "$ENV_FILE" ]; then
    echo "Copying $ENV_FILE to $REMOTE_DIR/.env"
    # shellcheck disable=SC2086
    ssh $SSH_OPTS "$OPENWRT_HOST" "umask 077 && cat > '$REMOTE_DIR/.env'" < "$ENV_FILE"
fi

# Launcher and boot service. The launcher runs the copied source directly, so
# no pip install is needed on the AP.
# shellcheck disable=SC2086
ssh $SSH_OPTS "$OPENWRT_HOST" \
    "REMOTE_DIR='$REMOTE_DIR' REMOTE_PYTHON='$REMOTE_PYTHON' REMOTE_AGENT='$REMOTE_AGENT' /bin/ash -s" <<'SETUP'
set -eu

if [ ! -f "$REMOTE_DIR/.env" ]; then
    echo "No $REMOTE_DIR/.env on the AP. Create one from .env.example and pass it with --env FILE." >&2
    exit 1
fi

cat > "$REMOTE_AGENT" <<LAUNCHER
#!$REMOTE_PYTHON
# Generated by scripts/deploy-openwrt.sh
import sys
sys.path.insert(0, "$REMOTE_DIR/src")
from open_omada_device_agent.cli import main
sys.exit(main())
LAUNCHER
chmod 755 "$REMOTE_AGENT"

# Boot service: install it, or replace it when the repository's version
# changed. A local AGENT_ARGS line (e.g. --debug) is kept.
SERVICE=/etc/init.d/open-omada-agent
NEW_SERVICE=$(mktemp)
sed -e "s|^AGENT_DIR=.*|AGENT_DIR=$REMOTE_DIR|" -e "s|^AGENT=.*|AGENT=$REMOTE_AGENT|" \
    "$REMOTE_DIR/scripts/openwrt/open-omada-agent.init" > "$NEW_SERVICE"
if [ -f "$SERVICE" ]; then
    args=$(grep '^AGENT_ARGS=' "$SERVICE" | head -n 1 || true)
    if [ -n "$args" ]; then
        awk -v line="$args" '/^AGENT_ARGS=/ { print line; next } { print }' \
            "$NEW_SERVICE" > "$NEW_SERVICE.args" && mv "$NEW_SERVICE.args" "$NEW_SERVICE"
    fi
fi
if [ ! -f "$SERVICE" ]; then
    mv "$NEW_SERVICE" "$SERVICE"
    chmod 755 "$SERVICE"
    "$SERVICE" enable
    echo "Installed and enabled the $SERVICE boot service"
elif ! cmp -s "$NEW_SERVICE" "$SERVICE"; then
    mv "$NEW_SERVICE" "$SERVICE"
    chmod 755 "$SERVICE"
    echo "Updated the $SERVICE boot service"
else
    rm -f "$NEW_SERVICE"
fi
SETUP

if [ "$RESTART" -eq 0 ]; then
    echo "Copied source. Restart skipped by --no-restart."
    exit 0
fi

# shellcheck disable=SC2086
ssh $SSH_OPTS "$OPENWRT_HOST" \
    "REMOTE_DIR='$REMOTE_DIR' REMOTE_PYTHON='$REMOTE_PYTHON' REMOTE_AGENT='$REMOTE_AGENT' AGENT_ARGS='$AGENT_ARGS' LOG_FILE='$LOG_FILE' /bin/ash -s" <<'REMOTE'
set -eu

if [ ! -x "$REMOTE_PYTHON" ]; then
    echo "Missing Python interpreter: $REMOTE_PYTHON" >&2
    exit 1
fi

if [ ! -x "$REMOTE_AGENT" ]; then
    echo "Missing agent entrypoint: $REMOTE_AGENT" >&2
    exit 1
fi

if ! command -v ubus >/dev/null 2>&1; then
    echo "Warning: ubus not found; OpenWrt telemetry and WLAN control will be limited" >&2
fi

if ! command -v uci >/dev/null 2>&1; then
    echo "Warning: uci not found; OpenWrt configuration control will be limited" >&2
fi

if ! command -v ndsctl >/dev/null 2>&1; then
    echo "Warning: ndsctl not found; openNDS portal telemetry/control will be limited" >&2
fi

SERVICE=/etc/init.d/open-omada-agent
if [ -x "$SERVICE" ] && "$SERVICE" enabled; then
    # procd respawns a killed agent, so a manual restart would leave two running.
    # The service uses its own AGENT, AGENT_ARGS and logd, not this script's
    # REMOTE_AGENT, AGENT_ARGS or LOG_FILE.
    SERVICE_AGENT=$(sed -n "s/^AGENT=//p" "$SERVICE" | tr -d "'\"")
    echo "Restarting the $SERVICE service (its own AGENT_ARGS apply; output in logread)"
    "$SERVICE" restart
    sleep 5
    if ! ps w | grep -v grep | grep -q "${SERVICE_AGENT:-open-omada-agent}"; then
        echo "open-omada-agent did not stay running. Last log lines:" >&2
        logread -e open_omada 2>/dev/null | tail -80 || true
        exit 1
    fi
    echo "Last log lines (logread -e open_omada):"
    logread -e open_omada 2>/dev/null | tail -80 || true
    exit 0
fi

agent_pids() {
    ps w | awk -v agent="$REMOTE_AGENT" '
        $1 ~ /^[0-9]+$/ && index($0, agent) && !index($0, " awk ") && !index($0, " grep ") {print $1}
    '
}

pids="$(agent_pids)"
if [ -n "$pids" ]; then
    echo "Stopping existing open-omada-agent process(es): $pids"
    for pid in $pids; do
        kill "$pid" 2>/dev/null || true
    done
    sleep 2
    remaining_pids="$(agent_pids)"
    if [ -n "$remaining_pids" ]; then
        echo "Force-stopping existing open-omada-agent process(es): $remaining_pids"
        for pid in $remaining_pids; do
            kill -9 "$pid" 2>/dev/null || true
        done
        sleep 1
    fi
fi

stamp="$(date +%Y%m%d-%H%M%S 2>/dev/null || echo now)"
if [ -f "$LOG_FILE" ]; then
    mv "$LOG_FILE" "$LOG_FILE.before-deploy-$stamp"
fi

cmd="cd '$REMOTE_DIR' && exec '$REMOTE_AGENT' $AGENT_ARGS >'$LOG_FILE' 2>&1 </dev/null"
if command -v setsid >/dev/null 2>&1; then
    setsid /bin/ash -c "$cmd" &
elif [ -x /sbin/start-stop-daemon ]; then
    /sbin/start-stop-daemon -S -b -x /bin/ash -- -c "$cmd"
else
    /bin/ash -c "$cmd" &
fi

sleep 5
new_pids="$(agent_pids)"
if [ -z "$new_pids" ]; then
    echo "open-omada-agent did not stay running. Last log lines:" >&2
    tail -80 "$LOG_FILE" 2>/dev/null || true
    exit 1
fi

echo "open-omada-agent running with PID(s): $new_pids"
echo "Last log lines from $LOG_FILE:"
tail -80 "$LOG_FILE" 2>/dev/null || true
REMOTE
