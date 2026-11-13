#!/bin/sh
# SPDX-License-Identifier: MIT
#
# udhcpc hook for one printer's own DHCP address. star-admin runs
#   busybox udhcpc -i <iface> -C -x 0x3d:<client-id> -s star-dhcp-alias.sh
# once per printer with `address = dhcp`, with PRINTER=<name> in the
# environment. udhcpc calls this script with the lease details in $ip, $mask,
# $lease, $router, $serverid and $interface.
#
# On a lease: add the address to the interface (no extra route: the Pi's own
# address already provides the subnet route) with the lease as its lifetime,
# and record it in $LEASE_DIR/lease-$PRINTER, where the bridge and the admin
# page read it. On failure: remove the address and the record.
#
# Nothing else is configured: no routes, DNS or hostname changes.

umask 022   # the bridge (another user) reads the lease record
DIR="${LEASE_DIR:-/run/star-lan-bridge}"
case "$PRINTER" in
    ''|*[!A-Za-z0-9_.-]*) logger -t star-dhcp "bad PRINTER name"; exit 1 ;;
esac
F="$DIR/lease-$PRINTER"
mkdir -p "$DIR"

old=""; oldmask=""
if [ -f "$F" ]; then
    old=$(sed -n 's/^ip=//p' "$F")
    oldmask=$(sed -n 's/^mask=//p' "$F")
fi

valid_ip() {
    echo "$1" | grep -Eq '^([0-9]{1,3}\.){3}[0-9]{1,3}$'
}

case "$1" in
    bound|renew)
        valid_ip "$ip" || { logger -t star-dhcp "$PRINTER: ignoring bad address '$ip'"; exit 1; }
        case "$mask" in ''|*[!0-9]*) mask=24 ;; esac
        life="${lease:-3600}"
        case "$life" in ''|*[!0-9]*) life=3600 ;; esac
        if [ -n "$old" ] && [ "$old" != "$ip" ]; then
            ip addr del "$old/$oldmask" dev "$interface" 2>/dev/null
        fi
        ip addr replace "$ip/$mask" dev "$interface" valid_lft "$life" preferred_lft "$life" noprefixroute
        printf 'ip=%s\nmask=%s\nrouter=%s\nlease=%s\nserver=%s\ninterface=%s\nobtained=%s\n' \
            "$ip" "$mask" "$router" "$life" "$serverid" "$interface" "$(date +%s)" > "$F.tmp" \
            && mv "$F.tmp" "$F"
        [ "$old" = "$ip" ] || logger -t star-dhcp "$PRINTER: $1 $ip/$mask on $interface, lease ${life}s"
        ;;
    leasefail|nak)
        if [ -n "$old" ]; then
            ip addr del "$old/$oldmask" dev "$interface" 2>/dev/null
            rm -f "$F"
        fi
        logger -t star-dhcp "$PRINTER: $1 on $interface"
        ;;
esac
exit 0
