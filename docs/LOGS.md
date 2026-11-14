# Reading the logs

Everything on the Pi logs to the **systemd journal**: the bridge, the admin
page, the printers' DHCP leases, Bluetooth, Wi-Fi and the system. The admin page
doesn't show logs; read them over SSH.

The journal is kept **across reboots and power cuts** on the SD card, capped at
**100 MB** and **one month**, with **2 GB** always left free (see
[SECURITY.md, Logs and storage](SECURITY.md#logs-and-storage)). Logs from before
a power cut are still there.

## Getting in

From a laptop on the same network as the Pi, with the SSH key used to set it up:

```bash
ssh <user>@<hostname or ip address>
```

The firewall only accepts SSH from private (venue LAN) addresses. The admin user
is in the `adm` group, so `journalctl` works **without sudo**. Plain log files in
`/var/log` (CUPS, apt) need `sudo`.

## Where each part logs

| Part | Command | What you'll see |
|---|---|---|
| **Bridge** (printing, discovery, Bluetooth link) | `journalctl -u star-lan-bridge` | print jobs, POS discovery queries, printer connects/disconnects, refusals and rate limits |
| **Admin page** (Wi-Fi, DHCP management) | `journalctl -u star-admin` | sign-ins, Wi-Fi joins, printer address requests; routine page views are not logged |
| **Printer DHCP leases** | `journalctl -t star-dhcp` | each lease, renewal or failure |
| **Bluetooth bindings** | `journalctl -u rfcomm-printer -u rfcomm-printer2` | `/dev/rfcommN` set up at boot |
| **Bluetooth stack** | `journalctl -u bluetooth` | BlueZ messages |
| **Wi-Fi and the Pi's address** | `journalctl -u NetworkManager` | connects, drops, roaming, the Pi's own DHCP lease |
| **SSH logins** | `journalctl -u ssh` | accepted and refused logins |
| **Kernel** | `journalctl -k` | drivers, Wi-Fi/Bluetooth chip, SD card errors |
| **CUPS** (local printing) | `sudo tail -50 /var/log/cups/error_log` | warnings only, file capped at 1 MB |
| **Manual updates** | `less /var/log/apt/history.log`, `sudo less /var/log/unattended-upgrades/unattended-upgrades.log` | what was installed, when |
| **Firewall** | `sudo nft list chain inet star_filter input \| grep counter` | how many packets were dropped (a count, not individual packets) |

### All three bridge sources together

The bridge and admin page are services (`-u`), but the DHCP lines are tagged
`star-dhcp` (`-t`). journalctl treats `-u` together with `-t` as "both at once",
which matches nothing (`-- No entries --`). To see them together, join the
filters with `+` ("or"):

```bash
journalctl -b _SYSTEMD_UNIT=star-lan-bridge.service + _SYSTEMD_UNIT=star-admin.service + SYSLOG_IDENTIFIER=star-dhcp
```

To save typing, add a shortcut once to `~/.bashrc` on the Pi:

```bash
echo "alias starlog='journalctl _SYSTEMD_UNIT=star-lan-bridge.service + _SYSTEMD_UNIT=star-admin.service + SYSLOG_IDENTIFIER=star-dhcp'" >> ~/.bashrc
source ~/.bashrc
```

Then `starlog -b`, `starlog -f`, `starlog -b -1` and so on work like `journalctl`.
The examples below use `starlog`; without the shortcut, use the full command above.

## Everyday recipes

```bash
# Watch live while you test (Ctrl-C to stop)
starlog -f

# Last 50 lines from the bridge
journalctl -u star-lan-bridge -n 50

# A time window
journalctl -u star-lan-bridge --since "2026-11-14 09:00" --until "2026-11-14 12:00"
journalctl -u star-lan-bridge --since "-2 h"

# Only one printer (lines carry the printer's name)
journalctl -u star-lan-bridge -g 'TSP143-2'

# Warnings and errors. The bridge and admin page log everything at "info"
# priority, so filter on the word rather than with -p:
journalctl -u star-lan-bridge -u star-admin -g 'WARNING|ERROR|Traceback'

# Print jobs only
journalctl -u star-lan-bridge -g '9100:'
```

## After a power cut or reboot

```bash
journalctl --list-boots                       # every boot kept, newest last
journalctl -b -1 -u star-lan-bridge           # the bridge in the boot before this one
starlog -b -1                                 # bridge, admin page and leases, boot before this one
journalctl -b -1 -e                           # end of the previous boot: what happened just before it went down
```

`-b 0` is the current boot, `-b -1` the one before, `-b -2` the one before that, and so on.

## Copying logs to your laptop

```bash
# From the laptop: the previous boot, everything, into a text file
ssh <user>@<hostname or ip address> 'journalctl -b -1 -o short-iso --no-pager' > pi-previous-boot.txt

# The bridge, admin page and leases over the last day (the alias isn't
# available in a one-off ssh command, so the full filter is spelled out)
ssh <user>@<hostname or ip address> 'journalctl --since "-1 day" -o short-iso --no-pager _SYSTEMD_UNIT=star-lan-bridge.service + _SYSTEMD_UNIT=star-admin.service + SYSLOG_IDENTIFIER=star-dhcp' > pi-bridge.txt
```

`-o short-iso` adds full dates to every line, which makes the logs easier to
share.

## What the important lines mean

| Log line | Meaning |
|---|---|
| `config /etc/star-lan-bridge.conf: TSP143=/dev/rfcomm0@192.168.1.134; ...` | Bridge started; each printer with its device and address |
| `[TSP143] printer: connected via /dev/rfcomm0, ASB enabled` | Bluetooth link to that printer is up |
| `[TSP143] printer: unreachable: printer didn't answer (off, asleep or out of range); retrying every 10-60s` | Printer off or out of range; the bridge keeps retrying by itself |
| `[TSP143] printer: link closed (EOF)` | The printer dropped the link (switched off, slept); reconnects automatically |
| `[TSP143] 9100: connection from 192.168.1.141:45328` | A POS (or CUPS, from `127.0.0.x`) started a print job |
| `[TSP143] 9100: ... finished, 48278 bytes received, 1 ASB-disable cmd(s) stripped, status now 23 86 02 ...` | Job delivered. A non-zero byte 7 (`02`) means the printer confirmed it. `23 86 08 00 00 08 ...` means out of paper |
| `[TSP143] discovery: answered 192.168.1.141:47383 as TSP143IIILAN @ 192.168.1.134` | A POS searched for or checked the printer (logged at most once a minute per POS) |
| `9100: refused 10.8.0.5 (not on a local network)` | Something outside the venue network tried to print; blocked |
| `9101: too many polls from ...` / `discovery: rate limit hit by ...` | A device is flooding the bridge; extra requests are dropped |
| `9100: ... closed: no data for 60 s` / `closed: job larger than 8 MB` | A connection broke the limits and was closed |
| `addresses changed {...} -> {...}; restarting` | A printer's DHCP address changed; the bridge restarts on the new one |
| `[TSP143-2] no address yet (address = dhcp)` | That printer is waiting for its DHCP lease |
| `star-dhcp: TSP143-2: bound 192.168.1.140/24 on wlan0, lease 43200s` | Printer 2 got its address |
| `dhcp: TSP143-2 lost its address 192.168.1.140; re-adding` | Something removed the address; star-admin put it back |
| `(... similar suppressed)` | Repeated messages are logged at most once a minute; this is how many were skipped |

## How much space the logs use

```bash
journalctl --disk-usage                       # at most 100 MB
df -h /                                       # the whole SD card
```

To make room by hand (not normally needed, because the limits do this):

```bash
sudo journalctl --vacuum-time=7d              # keep only the last week
```
