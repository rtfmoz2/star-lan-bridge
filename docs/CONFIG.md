# The config file

The bridge and the admin page (star-admin) both read
`/etc/star-lan-bridge.conf`. It has one section per printer, and an optional
`[network]` section for settings that apply to all of them. A commented
example is in [examples/star-lan-bridge.conf](../examples/star-lan-bridge.conf).

```ini
[network]
address   = next     # every printer takes the next free address
# next_from = 200    # optional: without it, the search starts just after the device's address

[printer:TSP143]
device  = /dev/rfcomm0
local   = 127.0.0.1

[printer:TSP143-2]
device  = /dev/rfcomm1
local   = 127.0.0.2
mac     = 00:11:62:00:00:02
```

With the device on `.10`, TSP143 takes `.11` and TSP143-2 `.12`, if no other
device is using them.

## Settings

### `[network]`: for all printers

The whole section is optional.

| Setting | Required | What it does |
|---|---|---|
| `address` | no | This provides an address setting for all printer interfaces. If a printer does not have an address setting it uses this. |
| `next_from` | no | The starting address for address allocation of printers. If not provided it defaults to the next ip after the device. |

With `address = next` here, as in the example above, the printer sections only
need `device` (and `local` and `mac` if you use them). `auto` and fixed
addresses can't go in `[network]`, because every printer would get the same
address.

### `[printer:NAME]`: one per printer

| Setting | Required | What it does |
|---|---|---|
| `[printer:NAME]` | yes | The printer's name, shown on the admin page and in the logs. Letters, digits, `.`, `_` and `-`, up to 32 characters. |
| `device` | yes | The Bluetooth serial port the bridge talks through, one per printer: `/dev/rfcomm0`, `/dev/rfcomm1`. On a test device it can point at a mock printer instead ([TESTING.md](TESTING.md)). |
| `address` | yes, unless `[network]` has one | The IP address the POS uses for this printer (choices below). Overrides `[network] address`. |
| `local` | no | A private address for printing from the device itself through CUPS, as `socket://<local>:9100`. Give each printer a different one: `127.0.0.1`, `127.0.0.2`. |
| `mac` | no | The hardware address reported when the POS searches for printers. Give each printer a different one; the printer's own Bluetooth MAC works well. Default: the MAC of the device's network interface. |

### `address`

Star apps find printers by IP address and use fixed ports (22222, 9100, 9101),
so every printer needs its own address on the network.

| Value | Address used |
|---|---|
| `auto` | The device's own address. Use it for one printer only. |
| `next` | A free address star-admin finds: by default the next one after the device's own, or from `[network] next_from` if set. It's remembered for each Wi-Fi network and never changes by itself. |
| `dhcp` | The printer's own lease from the router. If the router can't do that, it falls back to `next`. |
| a fixed address, e.g. `192.168.50.40` | That address, if you really need one. |

How `next` and `dhcp` work, and what happens when another device turns up on
a printer's address, is in [ADMIN.md, How printer addresses work](ADMIN.md#how-printer-addresses-work).

### `next_from`

Without `next_from`, a `next` printer takes the next free address after the
device's own: with the device on `.10`, `.11` if no device is using it, otherwise
`.12`, and so on. `next_from`, in the `[network]` section, changes where that
search starts for every `next` printer:

| Value | First address tried |
|---|---|
| *(not set)* | **The default:** just after the device's own address (device on `.10`: `.11`). |
| `after` | The same as not setting it. |
| a number from 1 to 254, e.g. `200` | That host on the device's network: `.200`. |
| a full address, e.g. `192.168.50.230` | That address. It must be on the device's network. |

If nothing is free within 32 addresses of the start, or the start isn't on
the device's network, it falls back to the default.

Routers usually hand out leases from the bottom of their range, just where
the default search looks. If you can see the router's settings, set
`next_from` above the end of its DHCP range: the router then never hands out
a printer's address. A printer that already has an address keeps it when
`next_from` changes.

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
