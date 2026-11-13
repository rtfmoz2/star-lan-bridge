#!/bin/bash
# SPDX-License-Identifier: MIT
#
# Lock down a star-lan-bridge host for deployment (see docs/SECURITY.md).
#
#   sudo ./scripts/harden.sh            apply everything; the firewall rolls
#                                       back by itself after 3 minutes unless
#                                       you confirm it
#   sudo ./scripts/harden.sh --confirm  keep the firewall (cancel the rollback)
#   sudo ./scripts/harden.sh --auto-updates
#                                       also turn on daily automatic Debian
#                                       security updates (off by default)
#
# 1. CUPS: local only (no sharing, no remote admin, no web interface)
# 2. SSH: key login only, no root, no forwarding (validated before reload)
# 3. Bluetooth: not discoverable, not pairable
# 4. Firewall: nftables default deny, venue LAN only (hardening/nftables.conf)
# 5. Logs and storage: journal kept across reboots but capped (100 MB, one
#    month, 2 GB always free), apt doesn't keep downloaded packages, CUPS logs
#    warnings only up to 1 MB.
# 6. Installs pending Debian security updates once, then turns automatic
#    updates OFF (set-and-forget: nothing changes on the device by itself).
#    With --auto-updates they stay on instead: security archive only, daily,
#    reboot at 03:30 when needed.
set -euo pipefail
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
[[ $EUID -eq 0 ]] || { echo "run as root (sudo)" >&2; exit 1; }

AUTO_UPDATES=0
[[ "${1:-}" == "--auto-updates" ]] && AUTO_UPDATES=1

if [[ "${1:-}" == "--confirm" ]]; then
    systemctl stop star-firewall-rollback.timer 2>/dev/null || true
    systemctl enable nftables.service >/dev/null
    echo "Firewall kept and enabled at boot."
    exit 0
fi

echo "== CUPS: local only"
if command -v cupsctl >/dev/null; then
    cupsctl --no-share-printers --no-remote-admin --no-remote-any WebInterface=no
    systemctl restart cups
fi

echo "== SSH"
install -m 644 "$REPO_DIR/hardening/sshd-hardening.conf" /etc/ssh/sshd_config.d/10-star-hardening.conf
sshd -t
systemctl reload ssh

echo "== Bluetooth: not discoverable or pairable"
bluetoothctl discoverable off >/dev/null || true
bluetoothctl pairable off >/dev/null || true

echo "== Firewall (rolls back in 3 minutes unless confirmed)"
command -v nft >/dev/null || apt-get install -y nftables
nft -c -f "$REPO_DIR/hardening/nftables.conf"
[[ -f /etc/nftables.conf && ! -f /etc/nftables.conf.pre-star ]] && cp /etc/nftables.conf /etc/nftables.conf.pre-star
install -m 644 "$REPO_DIR/hardening/nftables.conf" /etc/nftables.conf
systemctl stop star-firewall-rollback.timer 2>/dev/null || true
systemd-run --quiet --unit=star-firewall-rollback --on-active=180 \
    /usr/sbin/nft flush ruleset
nft -f /etc/nftables.conf

echo "== Logs and storage"
install -d /etc/systemd/journald.conf.d
install -m 644 "$REPO_DIR/hardening/journald-star.conf" /etc/systemd/journald.conf.d/90-star.conf
install -d -m 2755 -g systemd-journal /var/log/journal
systemctl restart systemd-journald
journalctl --flush
install -m 644 "$REPO_DIR/hardening/apt-no-cache" /etc/apt/apt.conf.d/02-star-no-cache
if command -v cupsctl >/dev/null; then
    cupsctl MaxLogSize=1m LogLevel=warn
fi

echo "== Security updates"
DEBIAN_FRONTEND=noninteractive apt-get install -y unattended-upgrades >/dev/null
# Security archive only (see hardening/50unattended-upgrades-star).
install -m 644 "$REPO_DIR/hardening/50unattended-upgrades-star" /etc/apt/apt.conf.d/52unattended-upgrades-star
apt-get update -qq
unattended-upgrade -v | grep -E "Packages that will be upgraded|All upgrades installed|No packages" || true
if [[ $AUTO_UPDATES -eq 1 ]]; then
    install -m 644 "$REPO_DIR/hardening/20auto-upgrades" /etc/apt/apt.conf.d/20auto-upgrades
    systemctl enable --now apt-daily.timer apt-daily-upgrade.timer unattended-upgrades.service >/dev/null
    echo "Automatic security updates: ON (daily, reboot at 03:30 if needed)"
else
    printf 'APT::Periodic::Update-Package-Lists "0";\nAPT::Periodic::Unattended-Upgrade "0";\n' \
        > /etc/apt/apt.conf.d/20auto-upgrades
    systemctl disable --now apt-daily.timer apt-daily-upgrade.timer unattended-upgrades.service >/dev/null 2>&1 || true
    echo "Automatic updates: OFF (pending security updates were installed once)"
fi
apt-get clean
[[ -f /var/run/reboot-required ]] && echo "An update needs a reboot: sudo systemctl reboot"

echo
echo "Done. From another machine, check you can still SSH in and print, then run:"
echo "  sudo $0 --confirm"
echo "within 3 minutes, or the firewall rules are removed again."
