#!/bin/bash
# SPDX-License-Identifier: MIT
#
# Prepare CUPS for a Star TSP100-series printer (optional companion to install.sh).
#
#   sudo ./scripts/setup-cups.sh --driver ~/Star_CUPS_Driver-3.16.0_linux.tar [--queue TSP143]
#
# 1. installs CUPS, cups-filters, Avahi and the driver build dependencies
# 2. masks colord: on headless Raspberry Pi OS it exits on start, and every job
#    then waits 2 x 25 s for D-Bus timeouts (49 s per receipt instead of 0.7 s)
# 3. builds Star's CUPS driver from the source inside Star's archive and
#    installs the rastertostar filters and PPDs (the download needs a Star
#    account: https://starmicronics.com/support/products/tsp100iii-support-page/)
# 4. turns on printer sharing and the web interface
# 5. creates a shared queue on socket://127.0.0.1:9100 (the bridge), 72 mm paper
#
# Run install.sh afterwards (or before; order doesn't matter).
set -euo pipefail

DRIVER=""
QUEUE="TSP143"
while [[ $# -gt 0 ]]; do
    case "$1" in
        --driver) DRIVER="${2:-}"; shift 2 ;;
        --queue) QUEUE="${2:-}"; shift 2 ;;
        -h|--help) sed -n '4,18p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 1 ;;
    esac
done
[[ $EUID -eq 0 ]] || { echo "run as root (sudo)" >&2; exit 1; }
[[ -f "$DRIVER" ]] || { echo "--driver must point at Star_CUPS_Driver-*_linux.tar" >&2; exit 1; }

echo "== packages"
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y \
    cups cups-filters bluez bluez-cups avahi-daemon gcc make libcups2-dev libcupsimage2-dev

echo "== colord (masked: not needed for a monochrome receipt printer)"
systemctl mask colord

echo "== Star driver"
BUILD="$(mktemp -d)"
trap 'rm -rf "$BUILD"' EXIT
tar -xf "$DRIVER" -C "$BUILD"
SRC_TGZ="$(find "$BUILD" -name 'Star_CUPS_Driver-src-*.tar.gz' | head -1)"
[[ -n "$SRC_TGZ" ]] || { echo "source tarball not found inside $DRIVER" >&2; exit 1; }
tar -xzf "$SRC_TGZ" -C "$(dirname "$SRC_TGZ")"
SRC_DIR="$(dirname "$SRC_TGZ")/Star_CUPS_Driver"
make -C "$SRC_DIR"
install -m 755 "$SRC_DIR"/install/rastertostar* /usr/lib/cups/filter/
install -d /usr/share/cups/model/star
install -m 644 "$SRC_DIR"/install/*.ppd /usr/share/cups/model/star/

echo "== CUPS sharing"
systemctl enable --now cups avahi-daemon
cupsctl --share-printers --remote-any --remote-admin WebInterface=yes
systemctl restart cups

echo "== queue $QUEUE"
# CUPS warns "Printer drivers are deprecated"; Star's PPD still works on CUPS 2.4.
lpadmin -p "$QUEUE" -E -v socket://127.0.0.1:9100 -m star/tsp143.ppd \
    -D "Star TSP143IIIBI (Bluetooth)" -o printer-is-shared=true -o PageSize=X72MMY200MM
lpadmin -d "$QUEUE"
lpstat -v "$QUEUE"
