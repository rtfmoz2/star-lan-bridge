#!/bin/bash
# SPDX-License-Identifier: MIT
#
# Remove star-lan-bridge.
#
#   sudo ./scripts/uninstall.sh [--cups-queue TSP143] [--keep-rfcomm]
#
# --cups-queue   point that CUPS queue back at serial:/dev/rfcomm0 so CUPS
#                keeps printing without the bridge
# --keep-rfcomm  leave rfcomm-printer.service installed (CUPS needs it)
set -euo pipefail

CUPS_QUEUE=""
KEEP_RFCOMM=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --cups-queue) CUPS_QUEUE="${2:-}"; shift 2 ;;
        --keep-rfcomm) KEEP_RFCOMM=1; shift ;;
        -h|--help) sed -n '4,11p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 1 ;;
    esac
done
[[ $EUID -eq 0 ]] || { echo "run as root (sudo)" >&2; exit 1; }

systemctl disable --now star-lan-bridge.service 2>/dev/null || true
rm -f /etc/systemd/system/star-lan-bridge.service
rm -rf /opt/star-lan-bridge

if [[ $KEEP_RFCOMM -eq 0 ]]; then
    systemctl disable --now rfcomm-printer.service 2>/dev/null || true
    rm -f /etc/systemd/system/rfcomm-printer.service
fi
systemctl daemon-reload

if [[ -n "$CUPS_QUEUE" ]] && lpstat -p "$CUPS_QUEUE" >/dev/null 2>&1; then
    lpadmin -p "$CUPS_QUEUE" -v "serial:/dev/rfcomm0?baud=115200"
    echo "CUPS queue $CUPS_QUEUE now prints to serial:/dev/rfcomm0"
fi

if id starbridge >/dev/null 2>&1; then
    userdel starbridge
fi
echo "star-lan-bridge removed"
