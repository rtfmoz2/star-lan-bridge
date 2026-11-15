#!/bin/bash
# SPDX-License-Identifier: MIT
#
# Collect everything needed to work out why the device didn't join a Wi-Fi
# network, into one archive you can copy off the device. Reads only; changes
# nothing unless you pass --debug-on or --debug-off.
#
#   ./tools/collect-wifi-logs.sh             collect into /tmp/wifi-logs-<date>.tar.gz
#   ./tools/collect-wifi-logs.sh --boots 3   only the last 3 boots (default: 10)
#   ./tools/collect-wifi-logs.sh --no-scan   don't ask for a fresh Wi-Fi scan
#   ./tools/collect-wifi-logs.sh --debug-on  detailed NetworkManager logging,
#                                            now and after reboots
#   ./tools/collect-wifi-logs.sh --debug-off back to normal logging
#
# Run it as your normal user; it asks for sudo where it needs it (saved
# network files, and the journal if your user isn't in the adm group).
#
# Passwords are never collected: saved network files are copied with their
# psk and password lines removed, and nmcli is never asked for secrets.
# The archive does contain network names (SSIDs), MAC addresses and IP
# addresses, so share it only with people you'd show those to.
#
# Copy it to a laptop afterwards, e.g.:
#   scp <user>@<pi-address>:/tmp/wifi-logs-*.tar.gz .
set -uo pipefail

IFACE=wlan0
BOOTS=10
SCAN=1
DEBUG_CONF=/etc/NetworkManager/conf.d/95-star-wifi-debug.conf
# DEFAULT keeps every normal log area; the rest add detail for joining Wi-Fi.
DEBUG_DOMAINS="DEFAULT,WIFI:DEBUG,WIFI_SCAN:DEBUG,SUPPLICANT:DEBUG,DEVICE:DEBUG,CORE:DEBUG,SETTINGS:DEBUG,DHCP4:DEBUG"

usage() { sed -n '4,25p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

debug_on() {
    printf '[logging]\nlevel=INFO\ndomains=%s\n' "$DEBUG_DOMAINS" | sudo tee "$DEBUG_CONF" >/dev/null
    # Apply now too, without restarting NetworkManager (that would drop Wi-Fi).
    sudo nmcli general logging level INFO domains "$DEBUG_DOMAINS"
    echo "Detailed NetworkManager logging is on, now and after reboots ($DEBUG_CONF)."
    echo "Turn it off afterwards: $0 --debug-off"
}

debug_off() {
    sudo rm -f "$DEBUG_CONF"
    sudo nmcli general logging level INFO domains DEFAULT
    echo "NetworkManager logging is back to normal."
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --boots) BOOTS="${2:-}"; shift ;;
        --no-scan) SCAN=0 ;;
        --debug-on) debug_on; exit 0 ;;
        --debug-off) debug_off; exit 0 ;;
        -h|--help) usage 0 ;;
        *) echo "unknown option: $1" >&2; usage 1 ;;
    esac
    shift
done
[[ "$BOOTS" =~ ^[0-9]+$ ]] || { echo "--boots needs a number" >&2; exit 1; }

STAMP=$(date +%Y%m%d-%H%M%S)
OUT=$(mktemp -d "/tmp/wifi-logs-$STAMP.XXXX")
chmod 700 "$OUT"
echo "Collecting into $OUT ..."

# The journal: without sudo if the user can read it (adm or systemd-journal group).
if journalctl -n1 -q >/dev/null 2>&1 && [[ -n "$(journalctl -n1 -q -u NetworkManager 2>/dev/null)" ]]; then
    J=(journalctl)
else
    sudo -v || { echo "sudo is needed to read the journal" >&2; exit 1; }
    J=(sudo journalctl)
fi

run() {  # run <file> <command...>: record the command and its output
    local f="$OUT/$1"; shift
    { echo "\$ $*"; "$@" 2>&1; echo; } >>"$f"
}

hex() { printf '%s' "$1" | od -An -tx1 | tr -d ' \n'; }

# 1. The system ---------------------------------------------------------------
run system.txt date -Is
run system.txt uptime
run system.txt cat /etc/os-release
run system.txt uname -a
run system.txt NetworkManager --version
run system.txt nmcli general status
run system.txt nmcli general logging
run system.txt "${J[@]}" --list-boots --no-pager
run system.txt systemctl --failed --no-legend

# 2. Wi-Fi country and radio ----------------------------------------------------
run radio.txt raspi-config nonint get_wifi_country
if command -v iw >/dev/null; then run radio.txt iw reg get; fi
run radio.txt cat /sys/module/cfg80211/parameters/ieee80211_regdom
if command -v rfkill >/dev/null; then run radio.txt rfkill list; fi
run radio.txt nmcli radio all

# 3. The interface right now --------------------------------------------------
run interface.txt nmcli -f all device show "$IFACE"
run interface.txt ip -br addr
run interface.txt ip route

# 4. Saved networks (no secrets) -----------------------------------------------
run saved.txt nmcli -f NAME,UUID,TYPE,AUTOCONNECT,AUTOCONNECT-PRIORITY,TIMESTAMP-REAL,ACTIVE,DEVICE con show
while IFS=: read -r uuid type; do
    [[ "$type" == "802-11-wireless" ]] || continue
    ssid=$(nmcli -g 802-11-wireless.ssid con show uuid "$uuid" 2>/dev/null)
    {
        echo "=== $uuid"
        echo "ssid (text): [$ssid]"
        echo "ssid (hex):  $(hex "$ssid")"
    } >>"$OUT/saved.txt"
    run saved.txt nmcli -f connection,802-11-wireless,802-11-wireless-security,ipv4 con show uuid "$uuid"
done < <(nmcli -t -f UUID,TYPE con show)

# Saved networks live in three places: keyfiles (the admin page writes these),
# netplan YAML (Raspberry Pi Imager and nmcli write these on Trixie) and the
# keyfiles NetworkManager generates from netplan at boot.
PROFILE_DIRS=(/etc/NetworkManager/system-connections /etc/netplan /run/NetworkManager/system-connections)
sudo -v 2>/dev/null
for d in "${PROFILE_DIRS[@]}"; do
    echo "\$ ls -l --time-style=full-iso $d"
    sudo ls -l --time-style=full-iso "$d" 2>&1
    echo
done >"$OUT/profile-files.txt"
for d in "${PROFILE_DIRS[@]}"; do
    dest="$OUT/profiles${d//\//_}"
    mkdir -p "$dest"
    sudo find "$d" -maxdepth 1 -type f -print0 2>/dev/null |
        while IFS= read -r -d '' f; do
            # keyfile "psk=..." and netplan "password: ..." lines lose their values
            sudo sed -E \
                -e 's/^([[:space:]]*(psk|password|wep-key[0-9]*|leap-password|private-key-password|pin)[[:space:]]*=).*/\1<removed>/' \
                -e 's/^([[:space:]]*(password|psk|key)[[:space:]]*:).*/\1 <removed>/' \
                -e 's/^([[:space:]]*"?(psk|password)"?[[:space:]]*:).*/\1 <removed>/' \
                "$f" >"$dest/$(basename "$f")"
        done
done
for f in NetworkManager.state seen-bssids timestamps; do
    [[ -r /var/lib/NetworkManager/$f ]] && cp "/var/lib/NetworkManager/$f" "$OUT/nm-$f.txt"
done

# 5. What the device can see --------------------------------------------------------
FIELDS=IN-USE,SSID,SSID-HEX,BSSID,MODE,CHAN,FREQ,RATE,SIGNAL,SECURITY,WPA-FLAGS,RSN-FLAGS
run scan.txt nmcli -f "$FIELDS" dev wifi list ifname "$IFACE" --rescan no
if [[ $SCAN -eq 1 ]]; then
    run scan.txt sudo nmcli -f "$FIELDS" dev wifi list ifname "$IFACE" --rescan yes
fi

# 6. Logs from each boot --------------------------------------------------------
mkdir -p "$OUT/boots"
mapfile -t BOOT_IDS < <("${J[@]}" --list-boots --no-pager 2>/dev/null | awk '$1 ~ /^-?[0-9]+$/ {print $1}' | tail -n "$BOOTS")
for b in "${BOOT_IDS[@]}"; do
    n=$(( b < 0 ? -b : b ))
    f="$OUT/boots/boot$( [[ $b -lt 0 ]] && echo "-" )$n.log"
    # One call per kind of match: journalctl ANDs a unit match (-u) with an
    # identifier match (-t), which would return nothing.
    {
        "${J[@]}" -b "$b" --no-pager -q -o short-iso-precise -u NetworkManager -u wpa_supplicant -u star-admin
        "${J[@]}" -b "$b" --no-pager -q -o short-iso-precise -t star-dhcp
    } 2>&1 | sort -s -k1,1 >"$f"
    "${J[@]}" -b "$b" --no-pager -q -o short-iso-precise -k 2>/dev/null |
        grep -iE 'brcmf|wlan0|cfg80211|regulatory|ieee80211' >"${f%.log}-kernel.log"
done
if [[ ${#BOOT_IDS[@]} -le 1 ]]; then
    echo "Only one boot in the journal: it isn't kept across reboots on this Pi" \
         "(scripts/harden.sh turns that on)." >>"$OUT/system.txt"
fi

# 7. A summary of the lines that usually explain it ------------------------------
PATTERN='ssid-not-found|no-secrets|secrets were required|4-way handshake|handshake failed|'
PATTERN+='Activation: \(?(starting|failed|successful)|auto-activating|blocking autoconnect|'
PATTERN+='tries left|autoconnect-retries|reason .(supplicant|no-secrets|ssid|ip-config|dhcp)|'
PATTERN+='state change: .* -> (failed|disconnected|activated)|CTRL-EVENT-(SSID-TEMP-DISABLED|ASSOC-REJECT|AUTH-REJECT|DISCONNECTED|CONNECTED)|'
PATTERN+='saved network|join .* failed|joined |regulatory|country'
{
    echo "Lines that usually explain a failed join, per boot (full logs in boots/)."
    echo
    for f in "$OUT"/boots/boot*.log; do
        [[ "$f" == *-kernel.log ]] && continue
        echo "=== $(basename "$f")"
        grep -aE "$PATTERN" "$f" | tail -n 200
        echo
    done
} >"$OUT/summary.txt"

# 8. Pack it up ----------------------------------------------------------------
TAR="/tmp/wifi-logs-$STAMP.tar.gz"
tar -czf "$TAR" -C "$(dirname "$OUT")" "$(basename "$OUT")"
chmod 600 "$TAR"
rm -rf "$OUT"
echo
echo "Done: $TAR ($(du -h "$TAR" | cut -f1))"
echo "Copy it to your laptop with:"
echo "  scp $(id -un)@<pi-address>:$TAR ."
