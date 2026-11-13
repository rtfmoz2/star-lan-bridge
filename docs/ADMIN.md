# Admin page: join Wi-Fi and see printer addresses

`star-admin` is a small web page on the Pi for setting it up on site without
SSH. It's built for venues where the Pi and the POS share the venue's **staff
WPA2 network**.

Open **`http://<hostname>.local`** (for example `http://jolly.local`) or the Pi's
IP address from a phone or laptop on the same network, and sign in.

| Section | What it shows or does |
|---|---|
| **Network** | Current Wi-Fi network and signal, every IP address on the Pi (marked DHCP or fixed), the gateway |
| **Printers** | Each printer's address to enter in the POS, how it got that address, Bluetooth link, status (Ready, Out of paper, Switched off or out of Bluetooth range) and its CUPS URL |
| **Join a Wi-Fi network** | Scan, tap a network, enter its password, then **Connect now** or **Save for later** |
| **Saved networks** | Networks the Pi will join on its own; **Forget** removes one (not the one in use) |

## Install

```bash
# Pi, from the repository
sudo ./scripts/install-admin.sh
sudo python3 /opt/star-lan-bridge/star_admin.py --set-password    # at least 10 characters
```

The page stays locked, showing only the command above, until a password is set.
The installer also advertises the page over Bonjour as *Printer bridge on
&lt;hostname&gt;*.

## Moving the Pi to a new venue

There is no setup hotspot. Use one of these:

1. **Save the venue's network before you leave.** On the current network, open the
   page, enter the venue's Wi-Fi name and password, and choose **Save for later**.
   At the venue, the Pi joins it by itself (saved networks get priority 10).
2. **Use a cable at the venue.** Plug the Pi's Ethernet port into the venue's
   router or switch, open `http://<hostname>.local` from a device on that network,
   and choose **Connect now**.

After **Connect now**:

* If you reached the page over the Pi's Wi-Fi, the page goes away while the Pi
  switches. Join the same network on your phone and reload `http://<hostname>.local`.
* If the connection fails (wrong password, out of range), the Pi deletes that
  profile and goes back to the network it was on. The page then explains what
  happened.
* The printers get addresses on the new network by themselves (next section),
  and the **Printers** section shows them. Enter those in the POS.

## How printer addresses work

Every address comes from DHCP; nothing is fixed.

| Config (`/etc/star-lan-bridge.conf`) | Address used |
|---|---|
| `address = auto` | The Pi's own DHCP address on the network it's using. Use this for one printer. |
| `address = dhcp` | The printer's own DHCP lease. |

For each `dhcp` printer, star-admin runs `busybox udhcpc` on the interface that
carries the default route. Wi-Fi access points only accept frames from the MAC
address that joined, so every lease uses the Pi's MAC. Each printer sends its
own **DHCP client ID** (option 61), `star-lan-bridge:<Pi MAC>:<printer name>`,
and routers treat each client ID as a separate device. The client ID never
changes, so the router hands back the same address each time.

`star-dhcp-alias.sh` (the udhcpc hook) adds the leased address to the interface
with the lease time as its lifetime, and records it in
`/run/star-lan-bridge/lease-<NAME>`. The bridge reads that file and restarts by
itself, within about 10 seconds, when any printer's address changes.

* **New network:** star-admin drops the old leases and requests new ones.
* **Address removed by NetworkManager** (for example on `nmcli dev reapply`):
  star-admin puts it back within 5 seconds while the lease is valid.
* **star-admin restarted:** it asks for the same address again (`udhcpc -r`) and
  nothing changes.

Tested on a UniFi network: printer 2 kept `192.168.1.140` across star-admin
restarts while printer 1 used the Pi's own `192.168.1.134`.

**Routers that lease by MAC only** give every client ID the Pi's own address.
The **Printers** section then shows printer 2 with the same address as printer 1,
and that network can run only one printer per Pi.

## Router reservations

**One printer per Pi** (only `address = auto`): a DHCP reservation for the
Pi's MAC address on the venue router is fine and recommended. The POS stores
the printer by IP, and a reservation keeps it fixed.

**More than one printer per Pi** (any `address = dhcp`): **don't** reserve an
address for the Pi's MAC on routers whose reservations work by MAC address.
That includes UniFi ("Fixed IP Address" on the client) and most home and small
business routers. Every lease the Pi asks for uses that same MAC, so the
reservation can apply to all of them: printer 2's lease may then get the Pi's
own reserved address, or keep being refused, and printer 2 disappears.

Leave those leases as ordinary DHCP instead. They are already stable: each
printer asks with a fixed client ID, and the router keeps handing back the same
address (UniFi kept 192.168.1.140 across reboots and restarts). Only DHCP
servers that can reserve by **client identifier** (option 61) can pin each
printer separately, for example ISC/Kea, dnsmasq's `id:` host entries or Windows
Server DHCP. There, reserve the Pi's MAC for the Pi and each printer's client
ID for that printer: type 0 followed by the text
`star-lan-bridge:<Pi MAC, lowercase, no colons>:<printer name>`, for example
`star-lan-bridge:dca6322592a3:TSP143-2`.

If an address does change, for example after a long power-off past the lease
time (12 hours on UniFi) or on a new network, the admin page shows the new one.
Search for printers again in the POS.

## Security

* The admin password is stored as a salted PBKDF2-SHA256 hash
  (240,000 iterations) in `/etc/star-admin/auth.json`, readable by root only.
* A sign-in creates a random session token in an `HttpOnly`, `SameSite=Strict`
  cookie that lasts 12 hours. Each client address gets 5 failed sign-ins per
  5 minutes.
* Every change is a JSON `POST` with the header `X-Star-Admin: 1`. Browsers
  can't send that header cross-site without a CORS preflight, which the server
  never approves, so other websites can't trigger changes. Responses forbid
  framing and set a strict Content Security Policy.
* Wi-Fi passwords are written straight into a root-only NetworkManager keyfile,
  never onto a command line or into a log.
* The page is **plain HTTP**. Use it on the staff network or over a cable, not
  on open guest Wi-Fi.
* The service runs as root because it edits NetworkManager connections, binds
  port 80 and adds addresses. systemd limits what it can write to Wi-Fi
  profiles, its password file and the lease records.

## Files

| Path | What |
|---|---|
| `/opt/star-lan-bridge/star_admin.py` | The service (Python standard library) |
| `/opt/star-lan-bridge/star-dhcp-alias.sh` | udhcpc hook for printer leases |
| `/etc/systemd/system/star-admin.service` | Unit (port 80) |
| `/etc/star-admin/auth.json` | Admin password hash |
| `/run/star-lan-bridge/lease-<NAME>` | Current lease per `dhcp` printer |
| `/etc/NetworkManager/system-connections/star-admin-*.nmconnection` | Networks added from the page |

Logs: `journalctl -u star-admin` (page and DHCP management) and
`journalctl -t star-dhcp` (each lease and renewal). See [LOGS.md](LOGS.md) for
reading them together with the bridge's, and from before a reboot.
