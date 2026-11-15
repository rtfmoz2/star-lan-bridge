# The config file

The bridge and the admin page (star-admin) both read
`/etc/star-lan-bridge.conf`. It has one section per printer. A commented
example is in [examples/star-lan-bridge.conf](../examples/star-lan-bridge.conf).

```ini
[printer:TSP143]
device  = /dev/rfcomm0
address = auto
local   = 127.0.0.1

[printer:TSP143-2]
device  = /dev/rfcomm1
address = next
next_from = 200
local   = 127.0.0.2
mac     = 00:11:62:00:00:02
```

## Settings

| Setting | Required | What it does |
|---|---|---|
| `[printer:NAME]` | yes | The printer's name, shown on the admin page and in the logs. Letters, digits, `.`, `_` and `-`, up to 32 characters. |
| `device` | yes | The Bluetooth serial port the bridge talks through, one per printer: `/dev/rfcomm0`, `/dev/rfcomm1`. On a test Pi it can point at a mock printer instead ([TESTING.md](TESTING.md)). |
| `address` | yes | The IP address the POS uses for this printer (choices below). |
| `next_from` | no | Only with `address = next`: where to start looking (below). |
| `local` | no | A private address for printing from the Pi itself through CUPS, as `socket://<local>:9100`. Give each printer a different one: `127.0.0.1`, `127.0.0.2`. |
| `mac` | no | The hardware address reported when the POS searches for printers. Give each printer a different one; the printer's own Bluetooth MAC works well. Default: the MAC of the Pi's network interface. |

### `address`

Star apps find printers by IP address and use fixed ports (22222, 9100, 9101),
so every printer needs its own address on the network.

| Value | Address used |
|---|---|
| `auto` | The Pi's own address. Use it for one printer only. |
| `next` | A free address star-admin finds, from `.200` up. It's remembered for each Wi-Fi network and never changes by itself. |
| `dhcp` | The printer's own lease from the router. If the router can't do that, it falls back to `next`. |
| a fixed address, e.g. `192.168.50.40` | That address, if you really need one. |

How `next` and `dhcp` work, and what happens when another device turns up on
a printer's address, is in [ADMIN.md, How printer addresses work](ADMIN.md#how-printer-addresses-work).

### `next_from`

| Value | First address tried |
|---|---|
| *(not set)* or `200` | `.200` on the Pi's network. Any host number from 1 to 254 works the same way. |
| a full address, e.g. `192.168.50.230` | That address, if it's on the Pi's network. |
| `after` | Just after the Pi's own address (Pi on `.10`: `.11`). |

If you can see the router's settings, start above the end of its DHCP range:
the router then never hands out a printer's address. A printer that already
has an address keeps it when `next_from` changes.

## Who reads it

* **star-admin** reads it to find which printers need an address (`next` or
  `dhcp`). It claims an address for each one on the network and records it in
  `/run/star-lan-bridge/`.
* **The bridge** reads it at startup, takes each printer's address from those
  records, and answers on that address. When an address changes, it restarts
  itself within about 10 seconds.

## After editing it

Restart both services:

```bash
sudo systemctl restart star-admin star-lan-bridge
```

A printer whose address isn't known yet is skipped, with a log line, until one
arrives. A mistake in the file, such as a missing `device` or a bad
`next_from`, stops the bridge with an error naming the section:

```bash
journalctl -u star-lan-bridge -n 20
```
