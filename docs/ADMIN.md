# Admin page: join Wi-Fi and see printer addresses

`star-admin` is a small web page on the device for setting it up on site without
SSH. It's built for venues where the device and the POS share the venue's **staff
WPA2 network**.

Open **`http://<hostname or IP address>`** from a phone or laptop on the same
network, and sign in.

Which one works depends on the device and network. The IP address always works.
The hostname works only where it can be looked up: as `<hostname>.local` on Apple
devices and most Linux machines, or as the plain hostname where the router hands
out local names. To find the IP address, look in the router's list of connected
devices; once you're signed in, the page's **Network** section shows every address
the device has.

| Section | What it shows or does |
|---|---|
| **Network** | Current Wi-Fi network and signal, every IP address on the device with whose it is ("this device" or a printer) and its source (DHCP, next available or fixed), the gateway |
| **Printers** | Each printer's address, how it got that address, Bluetooth link, status (Ready, Out of paper, Switched off or out of Bluetooth range) and its CUPS URL. A warning and a **Find another address** button if another device is using the printer's address |
| **Join a Wi-Fi network** | Scan, tap a network, enter its password, then **Connect now** or **Save for later** |
| **Saved networks** | Networks the device will join on its own; **Forget** removes one (not the one in use) |

## Install

```bash
# Device, from the repository
sudo ./scripts/install-admin.sh
sudo python3 /opt/star-lan-bridge/star_admin.py --set-password    # at least 10 characters
```

The page stays locked, showing only the command above, until a password is set.
The installer also advertises the page over Bonjour as *Printer bridge on
&lt;hostname&gt;*.

## Moving the device to a new venue

The device doesn't run a setup hotspot of its own. Use one of these:

1. **Save the venue's network before you leave.** On the current network, open the
   page, enter the venue's Wi-Fi name and password, and choose **Save for later**.
   At the venue, the device joins it by itself (saved networks get priority 10).
2. **Use a cable at the venue.** Plug the device's Ethernet port into the venue's
   router or switch, open `http://<hostname or IP address>` from a device on that
   network, and choose **Connect now**.
3. **Keep a phone hotspot saved as a fallback.** Before you leave, turn on
   your phone's hotspot and save it on the page like any other network. If the
   device doesn't join the venue network, turn the hotspot on: the device joins the
   phone, and the page opens on the same phone. Tap the venue network in the
   scan list (that copies its name exactly), enter the password and choose
   **Connect now**, then turn the hotspot off. Saved networks keep their
   passwords, so the hotspot doesn't replace the others.

Wi-Fi names must match exactly, including spaces and capitals. If the device still
won't join, see
[TROUBLESHOOTING.md](TROUBLESHOOTING.md#the-device-doesnt-join-the-venues-wi-fi).

After **Connect now**:

* If you reached the page over the device's Wi-Fi, the page goes away while the device
  switches. Join the same network on your phone and reload
  `http://<hostname or IP address>`.
* If the connection fails (wrong password, out of range), the device deletes that
  profile and goes back to the network it was on. The page then explains what
  happened.
* The printers get addresses on the new network by themselves (next section),
  and the **Printers** section shows them. Enter those in the POS.

## How printer addresses work

No address is fixed: each one comes from DHCP or is found free on the network.

| Config (`/etc/star-lan-bridge.conf`) | Address used |
|---|---|
| `address = auto` | The device's own DHCP address on the network it's using. Use this for one printer. |
| `address = next` | The next free address after the device's own, or from `next_from` if set. **Works with any router.** |
| `address = dhcp` | The printer's own DHCP lease. Falls back to `next` on routers that can't do it. |

### `next`: the next free address

**By default, the search starts just after the device's own address.** With the
device on `.10`, star-admin checks whether any other device is using `.11`. If none is,
the printer gets `.11`; otherwise it tries `.12`, and so on, up to 32
addresses. A second `next` printer takes the next free one after that.

**`next_from` changes where the search starts.** It applies to every `next`
printer, so it goes in the `[network]` section of the config, not in a
printer's section:

```ini
[network]
next_from = 200
```

The first `next` printer then tries `.200`, the second the next free one after
that, and so on. To put every printer on `next` without repeating it, set
`address = next` in `[network]` too ([CONFIG.md](CONFIG.md#network-for-all-printers)).

| `next_from` | First address tried |
|---|---|
| *(not set)* | **The default:** just after the device's own address (device on `.10`: `.11`). |
| `after` | The same as not setting it. |
| a number from 1 to 254, e.g. `200` | That host on the device's network: `.200`. On networks bigger than /24, it's `.200` in the device's own /24. |
| a full address, e.g. `192.168.50.230` | That address. It must be on the device's network. |

From the start it tries up to 32 addresses upward. If none of those is free,
or the start isn't on the device's network, it falls back to the default and
searches just after the device's own address.

**When to set it:** routers usually hand out leases from the bottom of their
range, just where the default search looks. If you can see the router's
settings, set `next_from` above the end of its DHCP range, and the router can
never hand out a printer's address. If you can't, a high start such as `200`
still makes a clash much less likely.

A printer that already has an address keeps it when `next_from` changes; use
**Find another address** (below) to move it.

* **How it checks:** an ARP probe (RFC 5227), sent from the device's Wi-Fi. Every
  device on the network answers ARP for its own address, even phones and
  laptops that ignore ping, so a device that's switched on is always found.
* **Same address next time:** the address is remembered per Wi-Fi network in
  `/var/lib/star-admin/claims.json`. After a reboot, or if the device's own
  address changes, the printer asks for the same address first, so the POS
  keeps finding it. It only moves if a device is using that address.
* **It never moves by itself.** The POS stores each printer's address, so a
  printer keeps its address once it has one. star-admin checks every two
  minutes whether another device also answers for it. If one does, it logs
  `next: another device is also using …` and the **Printers** section shows a
  warning under that printer, with a **Find another address** button. Either
  give the other device a different address, or press the button (twice, to
  confirm): the printer takes the next free address and the bridge restarts on
  it. Then delete the printer in the POS and add it again at the new address.
* **The router doesn't know about it.** It's a free address the router could
  still hand to a new device. Most routers check an address is unused before
  handing it out, and the device answers for it, so this rarely happens. A high
  `next_from` makes it rarer still. Where the router lets you, end its DHCP
  range below the printers' addresses, or reserve them.

### `dhcp`: the printer's own lease

For each `dhcp` printer, star-admin runs `busybox udhcpc` on the interface that
carries the default route. Wi-Fi access points only accept frames from the MAC
address that joined, so every lease uses the device's MAC. Each printer sends its
own **DHCP client ID** (option 61), `star-lan-bridge:<device MAC>:<printer name>`,
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
restarts while printer 1 used the device's own address.

**Routers that lease by MAC only** ignore the client ID and give every request
the device's own address. The hook refuses that lease, and star-admin switches the
printer to `next` for as long as the device stays on that network. The
**Printers** section says so under the printer, and the log shows
`refusing … router ignores the client ID`. Setting `address = next` in the
config skips the DHCP attempt on such networks.

## Router reservations

**One printer per device** (only `address = auto`): a DHCP reservation for the
device's MAC address on the venue router is fine and recommended. The POS stores
the printer by IP, and a reservation keeps it fixed.

**More than one printer per device** (any `address = dhcp`): **don't** reserve an
address for the device's MAC on routers whose reservations work by MAC address.
That includes UniFi ("Fixed IP Address" on the client) and most home and small
business routers. Every lease the device asks for uses that same MAC, so the
reservation can apply to all of them: printer 2's lease may then get the device's
own reserved address, or keep being refused, and printer 2 disappears.

Leave those leases as ordinary DHCP instead. They are already stable: each
printer asks with a fixed client ID, and the router keeps handing back the same
address (UniFi kept printer 2's lease across reboots and restarts). Only DHCP
servers that can reserve by **client identifier** (option 61) can pin each
printer separately, for example ISC/Kea, dnsmasq's `id:` host entries or Windows
Server DHCP. There, reserve the device's MAC for the device and each printer's client
ID for that printer: type 0 followed by the text
`star-lan-bridge:<device MAC, lowercase, no colons>:<printer name>`, for example
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
| `/run/star-lan-bridge/host` | The device's own address, for `address = auto` |
| `/var/lib/star-admin/claims.json` | Which `next` address each printer had on each network |
| `/etc/NetworkManager/system-connections/star-admin-*.nmconnection` | Networks added from the page |

Logs: `journalctl -u star-admin` (page, DHCP and `next` addresses) and
`journalctl -t star-dhcp` (each lease and renewal). See [LOGS.md](LOGS.md) for
reading them together with the bridge's, and from before a reboot.
