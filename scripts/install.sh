#!/bin/bash
# SPDX-License-Identifier: MIT
#
# Install star-lan-bridge on a Debian / Raspberry Pi OS host.
#
#   sudo ./scripts/install.sh --mac 00:11:62:AA:BB:CC [--channel 1] [--cups-queue TSP143]
#
# What it does:
#   1. creates the unprivileged system user "starbridge" (member of dialout)
#   2. installs star_lan_bridge.py to /opt/star-lan-bridge/
#   3. installs rfcomm-printer.service (binds the printer to /dev/rfcomm0) and
#      star-lan-bridge.service, then enables and starts both
#   4. with --cups-queue, repoints that existing CUPS queue at the bridge
#      (socket://127.0.0.1:9100) so CUPS and POS apps share one Bluetooth link
#
# The printer must already be paired and trusted (see docs/SETUP.md). Safe to
# re-run: every step either replaces files or skips work that's already done.
set -euo pipefail

MAC=""
CHANNEL=1
CUPS_QUEUE=""
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

usage() {
    sed -n '4,17p' "$0" | sed 's/^# \{0,1\}//'
    exit "${1:-0}"
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --mac) MAC="${2:-}"; shift 2 ;;
        --channel) CHANNEL="${2:-}"; shift 2 ;;
        --cups-queue) CUPS_QUEUE="${2:-}"; shift 2 ;;
        -h|--help) usage 0 ;;
        *) echo "unknown option: $1" >&2; usage 1 ;;
    esac
done

[[ $EUID -eq 0 ]] || { echo "run as root (sudo)" >&2; exit 1; }
[[ "$MAC" =~ ^([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}$ ]] || { echo "--mac AA:BB:CC:DD:EE:FF is required" >&2; exit 1; }
[[ "$CHANNEL" =~ ^[0-9]+$ ]] || { echo "--channel must be a number" >&2; exit 1; }
command -v rfcomm >/dev/null || { echo "rfcomm not found: apt install bluez" >&2; exit 1; }
command -v python3 >/dev/null || { echo "python3 not found" >&2; exit 1; }

if ! bluetoothctl info "$MAC" 2>/dev/null | grep -q "Paired: yes"; then
    echo "warning: $MAC is not paired yet; pair and trust it first (docs/SETUP.md)" >&2
fi

echo "== user"
if ! id starbridge >/dev/null 2>&1; then
    useradd --system --no-create-home --shell /usr/sbin/nologin -G dialout starbridge
fi

echo "== files"
install -d /opt/star-lan-bridge
install -m 755 "$REPO_DIR/star_lan_bridge.py" /opt/star-lan-bridge/
install -m 755 "$REPO_DIR/tools/star_probe.py" /opt/star-lan-bridge/
sed -e "s/@PRINTER_MAC@/$MAC/g" -e "s/@CHANNEL@/$CHANNEL/g" \
    "$REPO_DIR/systemd/rfcomm-printer.service" > /etc/systemd/system/rfcomm-printer.service
install -m 644 "$REPO_DIR/systemd/star-lan-bridge.service" /etc/systemd/system/

echo "== services"
systemctl daemon-reload
systemctl enable rfcomm-printer.service star-lan-bridge.service
systemctl restart rfcomm-printer.service
systemctl restart star-lan-bridge.service

if [[ -n "$CUPS_QUEUE" ]]; then
    echo "== CUPS queue $CUPS_QUEUE -> socket://127.0.0.1:9100"
    if lpstat -p "$CUPS_QUEUE" >/dev/null 2>&1; then
        lpadmin -p "$CUPS_QUEUE" -v socket://127.0.0.1:9100
    else
        echo "warning: CUPS queue $CUPS_QUEUE not found; create it first (docs/SETUP.md)" >&2
    fi
fi

sleep 3
echo "== status"
systemctl --no-pager --lines=0 status star-lan-bridge.service | head -3
journalctl -u star-lan-bridge -n 5 --no-pager -o cat
echo
echo "Test from another machine:  python3 tools/star_probe.py discover"
