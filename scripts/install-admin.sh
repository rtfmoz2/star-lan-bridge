#!/bin/bash
# SPDX-License-Identifier: MIT
#
# Install the star-admin web page (join Wi-Fi, show printer addresses).
#
#   sudo ./scripts/install-admin.sh
#
# Installs star_admin.py to /opt/star-lan-bridge/, the star-admin systemd
# service (port 80) and a Bonjour advert, then starts it. The page stays
# locked until you set a password:
#
#   sudo python3 /opt/star-lan-bridge/star_admin.py --set-password
set -euo pipefail
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
[[ $EUID -eq 0 ]] || { echo "run as root (sudo)" >&2; exit 1; }
command -v nmcli >/dev/null || { echo "NetworkManager (nmcli) is required" >&2; exit 1; }

install -d /opt/star-lan-bridge
install -m 755 "$REPO_DIR/star_admin.py" "$REPO_DIR/star_lan_bridge.py" "$REPO_DIR/star-dhcp-alias.sh" /opt/star-lan-bridge/
command -v busybox >/dev/null || apt-get install -y busybox
install -d -m 700 /etc/star-admin
install -m 644 "$REPO_DIR/systemd/star-admin.service" /etc/systemd/system/
if [[ -d /etc/avahi/services ]]; then
    install -m 644 "$REPO_DIR/avahi/star-admin.service" /etc/avahi/services/
fi
systemctl daemon-reload
systemctl enable star-admin.service
systemctl restart star-admin.service
sleep 2
systemctl --no-pager --lines=3 status star-admin.service | head -5
if [[ ! -s /etc/star-admin/auth.json ]]; then
    echo
    echo "Set the admin password now:"
    echo "  sudo python3 /opt/star-lan-bridge/star_admin.py --set-password"
fi
echo "Then open http://$(hostname).local/"
