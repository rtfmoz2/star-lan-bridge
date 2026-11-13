# Security

How a star-lan-bridge host is locked down for deployment on a venue network,
what that protects against, and what it doesn't. To apply everything on a Pi:

```bash
sudo ./scripts/harden.sh            # firewall rolls back after 3 minutes unless confirmed
# from another machine: check SSH, discovery and printing still work, then
sudo ./scripts/harden.sh --confirm
```

## Threat model

The Pi sits on a venue's **staff WPA2 network**. Anyone who knows that Wi-Fi
password, or controls any device on it, can send traffic to the Pi. The bridge
must keep printing for the POS, must not become a way into the Pi or the
network, and must not be easy to knock offline.

## The print data stream (TCP 9100, 9101, UDP 22222)

**The bridge never interprets client data.** Print data is copied to the
Bluetooth link with one 4-byte command (`ESC RS a 0`) removed. 9101 queries
are read and ignored. Discovery packets are only checked for the `STR_BCAST`
magic. Nothing from the network is executed, evaluated, deserialised or passed
to a shell. The code is Python (bounds-checked), standard library only.

| Protection | Detail |
|---|---|
| **Local network only** | Every connection and query is checked: loopback and addresses inside the Pi's own subnets are allowed, everything else is refused. Subnets are re-read every 30 s, so a new venue needs no change. |
| **No reflection or amplification** | Discovery replies (302 bytes per printer for a 28-byte query) only go to allowed sources, never to broadcast, multicast, unspecified or outside addresses. Rate limits: 5 queries/s per source (burst 20) and 100/s in total. Packets over 512 bytes are ignored. |
| **A connection can't hold a printer** | A 9100 client is closed after **60 s without data** or **5 minutes** in total, and a job over **8 MB** is cut off. Real receipts are tens of kilobytes. |
| **Status floods can't stall printing** | 9101 queries are handled without blocking. A silent client costs nothing and is answered after 0.5 s. Rate limits: 20 polls/s per source and 100/s in total, with at most 64 waiting. Fresh status is requested from the printer at most once every 2 s. |
| **Printer side** | Status frames are parsed with length checks; garbage is skipped and the receive buffer is capped at 4 KB. |
| **Logs can't be flooded** | Refusals, rate-limit hits and odd packets are logged at most once a minute per kind and source, with a count of how many were suppressed. |
| **A crash restarts cleanly** | A failure in one printer's loop exits the process, and systemd restarts it within 3 s. |

`tests/test_fuzz.py` runs the real bridge loop against a fake printer (a
pseudo-terminal) and covers: 40 random jobs (random bytes and dense Star escape
sequences, cut at random points) that must arrive byte-for-byte, minus
`ESC RS a 0`; an idle client blocking the queue; an oversized job; 30 reset
connections; garbage and oversized 9101 queries; 40 silent 9101 clients,
while print data still flows; 20 KB of garbage from the printer; and 500 junk
UDP packets. Two weaknesses it found were fixed: silent 9101 clients blocked
the printer loop for 0.2 s each, and each poll could wait 0.6 s on a printer
that didn't answer.

### What anyone on the venue network can still do

A real Star LAN printer accepts print data from any device on its network,
and so does the bridge (restricted to the local network). So a device on the
staff network can still:

* **print anything, or waste paper;**
* **open a cash drawer** connected to the printer;
* **send Star configuration commands** (for example memory-switch settings)
  to the printer itself.

The bridge doesn't filter Star commands: the raster data stream can't be told
apart from commands reliably without a full Star parser, and a filter that
guessed wrong would corrupt receipts. The protection is the network: keep the
POS and Pi on a staff network whose password isn't shared with guests, with
client isolation off only on that network.

## Process sandbox

Both services run under systemd sandboxing. `systemd-analyze security` exposure
scores: **star-lan-bridge 1.8**, **star-admin 2.4** (0 is fully locked down; an
unsandboxed service scores about 9.6).

| | star-lan-bridge | star-admin |
|---|---|---|
| User | `starbridge` (unprivileged, `dialout` for RFCOMM) | root |
| Capabilities | **none** | `CAP_NET_BIND_SERVICE`, `CAP_NET_ADMIN`, `CAP_NET_RAW` only |
| Devices | RFCOMM ports only (`DevicePolicy=closed`) | none (`PrivateDevices`) |
| Network | localhost, link-local, multicast and private ranges only: **cannot reach the internet** | same, plus DHCP's `0.0.0.0` and `255.255.255.255` |
| Filesystem | read-only (`ProtectSystem=strict`), no home directories | read-only except Wi-Fi profiles, its password file and lease records |
| Other | `NoNewPrivileges`, system-call filter `@system-service`, `MemoryDenyWriteExecute`, no namespaces, kernel tunables/modules/logs protected | same |

So even if someone found a flaw in the bridge's code, they would land in a
process with no capabilities, no writable files, access to two RFCOMM ports,
and no route to the internet.

## The rest of the Pi (`scripts/harden.sh`)

| Area | Setting |
|---|---|
| **Firewall** (`hardening/nftables.conf`) | Inbound default **drop**. Allowed only from private and link-local addresses: TCP 22, 80, 9100, 9101; UDP 22222 and mDNS 5353. Also DHCP replies, ICMP, and replies to the Pi's own connections. Forwarding is off. |
| **CUPS** | Local only: no sharing, no remote admin, no web interface; port 631 is closed. Local queues still print through the bridge. |
| **SSH** | Key login only; no root login, X11, TCP or agent forwarding, or tunnels; 3 tries per connection, 20 s to log in. Can be **turned off entirely** after deployment from the admin page's `/sshctl` (next section). |
| **Bluetooth** | Not discoverable and not pairable, so nothing new can pair with the Pi. Pairing a printer needs `pairable on` first (SETUP.md). |
| **Updates** | **Set and forget:** `harden.sh` installs all pending Debian **security** updates once, then turns automatic updates **off** (apt timers and unattended-upgrades disabled), so the device never changes by itself. With `--auto-updates` they stay on: security archive only, daily, rebooting at 03:30 only when needed. |

## Logs and storage

Everything the bridge, the admin page and the DHCP leases do goes to the systemd
journal. There's no rsyslog. `harden.sh` makes sure logs can't fill the SD card:

| What | Setting |
|---|---|
| **Journal** (`hardening/journald-star.conf`) | Kept **across reboots and power cuts** on the SD card, overriding Raspberry Pi OS's RAM-only default. Limits: **100 MB**, **one month**, and at least **2 GB** always left free, whichever is hit first. Compressed; written in batches every 5 minutes (errors immediately); at most 1000 lines per 30 s per service, so one runaway program can't flood it. |
| **apt** (`hardening/apt-no-cache`) | Downloaded `.deb` files are deleted after installing (today's updates had left 195 MB). |
| **CUPS** | Warnings only, log capped at 1 MB. |
| **`/var/log`** | Small files (apt, dpkg, CUPS) rotated by logrotate (enabled by default). |
| **Admin page** | Doesn't log routine page loads or its 5-second refresh; sign-ins, changes and errors are logged. |
| **`/tmp`, `/run`** | RAM disks; cleared at every boot. |

Reading the logs:

```bash
journalctl -u star-lan-bridge -b                                  # the bridge, this boot
journalctl -u star-lan-bridge -b -1                               # the boot before (e.g. before a power cut)
journalctl --list-boots                                           # which boots are kept
journalctl --disk-usage                                           # how much space logs use (max 100 MB)
```

More recipes, including seeing all the bridge's logs together, are in [LOGS.md](LOGS.md).

## Admin page

See [ADMIN.md](ADMIN.md#security): a salted PBKDF2 password hash, rate-limited
sign-in, `SameSite=Strict` session cookies, and a custom header required on
every change. It's reachable from the venue network only (firewall). By choice
it is **plain HTTP**, so the admin and Wi-Fi passwords cross the staff network
unencrypted when you use it. Keep its use short, or use a cable.

## Turning SSH off after deployment (`/sshctl`)

Once a Pi is installed at a venue, nobody needs to log in to it, so SSH can be
switched off completely: then there's no login service on the network at all.

Open **`http://<hostname>.local/sshctl`**. The page isn't linked from the admin
page, but it **requires the same admin sign-in**; signing in from it returns you
to it. It shows **SSH Connections:** with a badge:

| Badge | Meaning |
|---|---|
| `Accepted` (green) | SSH is on: new logins accepted |
| `Denied` (red) | SSH is off: new logins refused |
| `Turning off…` / `Turning on…` (amber) | Change in progress (a few seconds) |

Untick **Allow SSH logins** (and confirm) to turn it off; tick it to turn it back on.

* **Off** stops `ssh.service` and disables it at boot, and makes sure
  `ssh.socket` (socket activation) is off too, so it stays off after reboots
  and power cuts. Port 22 then has nothing listening, and connections are
  refused. **Sessions already open are not ended;** they last until they
  disconnect.
* **On** starts and re-enables `ssh.service`. The hardening above still
  applies: key login only, no root.
* **Every change is logged** with the address it came from:

  ```bash
  journalctl -u star-admin -g 'ssh: '
  # ... WARNING ssh: disable requested by 192.168.1.233
  # ... WARNING ssh: now off
  ```

* The admin page and the printers are unaffected either way.

**Getting SSH back without the page.** Raspberry Pi OS turns SSH on at boot if
a file named `ssh` is on the SD card's boot partition (`sshswitch.service`, left
enabled). Power the Pi off, put the card in a laptop, create an empty file named
`ssh` (or `ssh.txt`, easier on Windows) on the boot partition (it shows up as
`bootfs`), put the card back and start the Pi. At boot, Raspberry Pi OS deletes
the file and turns SSH on and enables it at boot, with the same key-only
settings. `/sshctl` then shows `Accepted`.

**Notes**

* "Hidden" only means unlinked: the page is in this repository's source code.
  The admin password is what protects it.
* The page runs inside star-admin's sandbox, where `/etc` is read-only. It calls
  `systemctl` with `SYSTEMCTL_SKIP_SYSV=1` so Debian's SysV init-script
  mirroring (which writes `/etc/rc*.d`) is skipped. Without that, turning SSH
  off failed with "Read-only file system". Those links are ignored anyway,
  because ssh has a native systemd unit.
* The firewall still allows TCP 22 from the venue LAN, so turning SSH back on
  needs no firewall change. With SSH off, nothing answers there.

## Remaining risks and recommendations

* **Physical access.** The SD card isn't encrypted, so anyone who takes it can
  read the Wi-Fi passwords and SSH keys. Mount the Pi out of reach of the public.
* **The admin account has passwordless sudo.** Anyone with that SSH key is
  root. Keep the key on admin machines only, or turn SSH off at `/sshctl` once
  the Pi is installed (above). You can also remove
  `/etc/sudoers.d/010_<user>-nopasswd` after deployment; later maintenance
  then needs the account password.
* **The admin page is plain HTTP** (above). That includes `/sshctl`: anyone who
  learns the admin password, for example by watching the staff network, could
  turn SSH back on. Keys are still required to log in.
* **Staff Wi-Fi is the boundary.** Anyone who has its password can print and
  open the cash drawer.
* **No automatic updates.** Security fixes published after deployment don't
  arrive by themselves; the device relies on the firewall, the sandbox and the
  staff network. On maintenance visits, run
  `sudo apt update && sudo unattended-upgrade -v` (security only), then
  reboot. Raspberry Pi archive packages (kernel, firmware) are never updated
  automatically; `sudo apt full-upgrade` covers them when you choose to.
