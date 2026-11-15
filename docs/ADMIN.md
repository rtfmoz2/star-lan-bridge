# Admin page: join Wi-Fi and see printer addresses

`star-admin` is a small web page on the Pi for setting it up on site without
SSH. It's built for venues where the Pi and the POS share the venue's **staff
WPA2 network**.

Open **`http://<hostname or IP address>`** from a phone or laptop on the same
network, and sign in.

Which one works depends on the device and network. The IP address always works.
The hostname works only where it can be looked up: as `<hostname>.local` on Apple
devices and most Linux machines, or as the plain hostname where the router hands
out local names. To find the IP address, look in the router's list of connected
devices; once you're signed in, the page's **Network** section shows every address
the Pi has.

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

The Pi doesn't run a setup hotspot of its own. Use one of these:

1. **Save the venue's network before you leave.** On the current network, open the
   page, enter the venue's Wi-Fi name and password, and choose **Save for later**.
   At the venue, the Pi joins it by itself (saved networks get priority 10).
2. **Use a cable at the venue.** Plug the Pi's Ethernet port into the venue's
   router or switch, open `http://<hostname or IP address>` from a device on that
   network, and choose **Connect now**.
3. **Keep a phone hotspot saved as a fallback.** Before you leave, turn on
   your phone's hotspot and save it on the page like any other network. If the
   Pi doesn't join the venue network, turn the hotspot on: the Pi joins the
   phone, and the page opens on the same phone. Tap the venue network in the
   scan list (that copies its name exactly), enter the password and choose
   **Connect now**, then turn the hotspot off. Saved networks keep their
   passwords, so the hotspot doesn't replace the others.

Wi-Fi names must match exactly, including spaces and capitals. If the Pi still
won't join, see
[TROUBLESHOOTING.md](TROUBLESHOOTING.md#the-pi-doesnt-join-the-venues-wi-fi).

After **Connect now**:

* If you reached the page over the Pi's Wi-Fi, the page goes away while the Pi
  switches. Join the same network on your phone and reload
  `http://<hostname or IP address>`.
* If the connection fails (wrong password, out of range), the Pi deletes that
  profile and goes back to the network it was on. The page then explains what
  happened.
* The printers get addresses on the new network by themselves (next section),
  and the **Printers** section shows them. Enter those in the POS.

## How printer addresses work

No address is fixed: each one comes from DHCP or is found free on the network.

| Config (`/etc/star-lan-bridge.conf`) | Address used |
|---|---|
| `address = auto` | The Pi's own DHCP address on the network it's using. Use this for one printer. |
| `address = next` | The first free address after the Pi's own. **Works with any router.** |
| `address = dhcp` | The printer's own DHCP lease. Falls back to `next` on routers that can't do it. |

### `next`: the next free address

With the Pi on `.10`, star-admin checks whether any device is using `.11`. If
none is, the printer gets `.11`; otherwise it tries `.12`, and so on, up to 32
addresses past the Pi's own. A second `next` printer takes the next free one
after that.

* **How it checks:** an ARP probe (RFC 5227), sent from the Pi's Wi-Fi. Every
  device on the network answers ARP for its own address, even phones and
  laptops that ignore ping, so a device that's switched on is always found.
* **Same address next time:** the address is remembered per Wi-Fi network in
  `/var/lib/star-admin/claims.json`. After a reboot, or if the Pi's own
  address changes, the printer asks for the same address first, so the POS
  keeps finding it. It only moves if a device is using that address.
* **If a device turns up on it later:** star-admin checks every two minutes.
  If another device answers for the printer's address, the printer moves to the
  next free one and the bridge restarts on it. It answers the POS's printer
  search at the new address straight away; select it again there if the POS
  stored the old address. The log says `next: another device is now using …`.
* **The router doesn't know about it.** It's a free address the router could
  still hand to a new device. Most routers check an address is unused before
  handing it out, and the Pi answers for it, so this rarely happens; if it
  does, the check above moves the printer. Where the router lets you, shrink
  its DHCP range so it ends below the Pi's addresses, or reserve them.

### `dhcp`: the printer's own lease

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

Tested on a UniFi network: printer 2 kept the same lease across star-admin
restarts while printer 1 used the Pi's own address.

**Routers that lease by MAC only** ignore the client ID and give every request
the Pi's own address. The hook refuses that lease, and star-admin switches the
printer to `next` for as long as the Pi stays on that network. The
**Printers** section says so under the printer, and the log shows
`refusing … router ignores the client ID`. Setting `address = next` in the
config skips the DHCP attempt on such networks.

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
address (UniFi kept printer 2's lease across reboots and restarts). Only DHCP
servers that can reserve by **client identifier** (option 61) can pin each
printer separately, for example ISC/Kea, dnsmasq's `id:` host entries or Windows
Server DHCP. There, reserve the Pi's MAC for the Pi and each printer's client
ID for that printer: type 0 followed by the text
`star-lan-bridge:<Pi MAC, lowercase, no colons>:<printer name>`, for example
`star-lan-bridge:dca632000001:TSP143-2`.

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
| `/run/star-lan-bridge/lease-<NAME>` | Current address per `dhcp` or `next` printer |
| `/run/star-lan-bridge/host` | The Pi's own address, for `address = auto` |
| `/var/lib/star-admin/claims.json` | Which `next` address each printer had on each network |
| `/etc/NetworkManager/system-connections/star-admin-*.nmconnection` | Networks added from the page |

Logs: `journalctl -u star-admin` (page, DHCP and `next` addresses) and
`journalctl -t star-dhcp` (each lease and renewal). See [LOGS.md](LOGS.md) for
reading them together with the bridge's, and from before a reboot.
