# star-lan-bridge

Make a **Bluetooth-only Star receipt printer** look like a **Star LAN printer**
on your network, so StarIO-based POS apps can find it, print to it and show it
as connected.

```
POS app / laptop ──Wi-Fi──▶ Raspberry Pi ──Bluetooth SPP──▶ Star TSP143IIIBI
                            ├─ UDP 22222  Star discovery
                            ├─ TCP 9100   raw print + live status
                            ├─ TCP 9101   status polling
                            └─ CUPS :631  (optional, for AirPrint / IPP)
```

The Pi answers Star's discovery broadcast as a **TSP143IIILAN**, relays print
data to the printer over `/dev/rfcomm0`, and passes the printer's real status
frames back. POS apps then confirm each job and don't reprint. It's a single
Python file with no dependencies beyond the standard library.

**Several printers.** One Pi can serve more than one Bluetooth printer. Each
gets its own IP address from DHCP, its own `/dev/rfcommN` and its own CUPS
queue, and appears to POS apps as a separate Star LAN printer:

```
Pi's DHCP address    ──▶ /dev/rfcomm0 ──▶ printer 1   (address = auto, CUPS 127.0.0.1:9100)
printer's own lease  ──▶ /dev/rfcomm1 ──▶ printer 2   (address = dhcp, CUPS 127.0.0.2:9100)
```

**Web admin page.** `star-admin` serves a password-protected page at
`http://<hostname>.local` for joining a Wi-Fi network and seeing each printer's
address and status, so a Pi can move to a new venue without SSH. See
[docs/ADMIN.md](docs/ADMIN.md).

See [SETUP.md §7](docs/SETUP.md#7-several-printers) and
[examples/star-lan-bridge.conf](examples/star-lan-bridge.conf).

## Tested with

| Component | Version |
|---|---|
| Printers | 2 × Star TSP143IIIBI (TSP100III Bluetooth), 72 mm, on one Pi |
| Bridge host | Raspberry Pi 4 Model B on Wi-Fi, Raspberry Pi OS Trixie Lite 64-bit, BlueZ 5.82, Python 3.13 |
| POS | SunMi D3 Pro, SunMi OS 4.0.79, **Next Order POS 6.0.0** |
| Desktop | macOS printing through CUPS 2.4 + Star CUPS Driver 3.16.0 |

Results on that setup:

| | |
|---|---|
| Discovery reply | 302 bytes in about 1 ms |
| 9101 status poll | 22 bytes in about 5 ms |
| POS receipt, end to end | about 3 s, one copy |
| CUPS receipt (after the colord fix) | 0.7 s |
| Two printers, broadcast discovery | two replies, each from its own IP |

Other StarIO clients (Star's own apps, Shopify POS, Square and so on) use the same
protocol, but haven't been tested with this bridge. Reports are welcome.

## Quick start

```bash
# on the Pi, with the printer already paired and trusted (docs/SETUP.md §3)
git clone https://github.com/rtfmoz2/star-lan-bridge.git
cd star-lan-bridge
sudo ./scripts/install.sh --mac 00:11:62:AA:BB:CC --channel 1
```

```bash
# from any machine on the same network (prints nothing)
python3 tools/star_probe.py discover
python3 tools/star_probe.py status --host <pi-ip>
```

Then search for Star LAN printers in your POS app.

`install.sh` sets up one printer. To add more, write `/etc/star-lan-bridge.conf`
and give each printer its own address; see [SETUP.md §7](docs/SETUP.md#7-several-printers).

## Documentation

* **[docs/SETUP.md](docs/SETUP.md)**: full setup, from flashing the Pi to
  pairing, the bridge, the optional CUPS queue, POS configuration and
  adding more printers
* **[docs/ADMIN.md](docs/ADMIN.md)**: the web page (join Wi-Fi, see addresses),
  how printers get DHCP addresses, and its security
* **[docs/SECURITY.md](docs/SECURITY.md)**: threat model, how the bridge handles
  hostile data, the service sandbox, firewall and Pi lockdown (`scripts/harden.sh`)
* **[docs/PROTOCOL.md](docs/PROTOCOL.md)**: Star Discovery (UDP 22222), ASB
  status frames, the 9100 and 9101 channels, and why `ESC RS a 0` is
  stripped, with each fact tagged by source
* **[docs/LOGS.md](docs/LOGS.md)**: reading the logs over SSH: where each part
  logs, everyday recipes, logs from before a power cut, copying them to a
  laptop, and what the important lines mean
* **[docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md)**: duplicate prints,
  "not connected", slow CUPS jobs, Bluetooth link and pairing errors, a second
  printer that doesn't show up, paper-out status
* **[CHANGELOG.md](CHANGELOG.md)**: what changed in each version

## Repository layout

```
star_lan_bridge.py            the bridge (stdlib only)
star_admin.py                 web admin: join Wi-Fi, show addresses, keep printer DHCP leases
star-dhcp-alias.sh            udhcpc hook that adds a printer's leased address
tools/star_probe.py           read-only StarIO client: discover / status / etb
examples/star-lan-bridge.conf two-printer config (/etc/star-lan-bridge.conf)
systemd/star-lan-bridge.service
systemd/star-admin.service    web admin on port 80
avahi/star-admin.service      Bonjour advert for the admin page
systemd/rfcomm-printer.service    template; install.sh fills in MAC and channel
scripts/install.sh            install bridge + rfcomm binding, optionally repoint CUPS
scripts/install-admin.sh      install the web admin page
scripts/harden.sh             lock the Pi down for deployment (firewall, SSH, CUPS, updates)
hardening/                    firewall, SSH and update settings used by harden.sh
scripts/setup-cups.sh         optional: CUPS + Star driver build + shared queue
scripts/uninstall.sh
tests/                        unit tests: python3 -m unittest discover tests
docs/                         setup, protocol and troubleshooting docs
```

## Limitations

* **One Bluetooth connection.** The printer accepts a single Bluetooth
  connection and the bridge holds it, so phones can't pair with the printer
  directly while the bridge runs.
* **One print client at a time.** Further clients wait in the TCP backlog,
  which is fine for a receipt printer.
* **Same subnet only.** Discovery is broadcast-based, so the POS must be on the
  bridge's subnet. Turn off Wi-Fi client isolation.
* **One IP per printer.** Star clients find printers by IP and the ports are
  fixed, so each extra printer needs its own DHCP lease. The Pi gets one per
  printer by sending a different DHCP client ID. Routers that lease by MAC
  address only can't do this, so on those networks run one printer per Pi
  (see [ADMIN.md](docs/ADMIN.md#how-printer-addresses-work)).
* **Plain-HTTP admin page.** Use it on the venue's staff network or a cable,
  not on open guest Wi-Fi.
* **Anyone on the staff network can print.** As with a real Star LAN printer,
  that includes opening a cash drawer. The staff Wi-Fi password is the
  boundary (see [SECURITY.md](docs/SECURITY.md)).
* **Identity.** The bridge always reports a TSP143IIILAN. By default it uses
  the host's own MAC; set `mac` per printer to keep printers distinct.
* **Byte filtering.** The bridge removes the exact bytes `1B 1E 61 00` from
  print data (see [PROTOCOL.md §4.3](docs/PROTOCOL.md#43-why-esc-rs-a-0-is-stripped)).
* **Linux only.** The bridge uses termios and Linux ioctls. The probe tool also
  runs on macOS.
* **Epson discovery.** UDP 3289 `EPSONQ` queries from POS apps aren't answered.

## Credits

The Star Discovery reply template, the ASB framing and the 9101 behaviour are
based on the reverse-engineering notes in
[gnaunited/tsp143-bridge](https://github.com/gnaunited/tsp143-bridge), a
USB-to-Wi-Fi bridge for the TSP143IIIU (MIT). See
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md). This project is an
independent implementation for Bluetooth printers.

Star, StarIO, StarPRNT and TSP are trademarks of Star Micronics Co., Ltd. This
project isn't affiliated with or endorsed by Star Micronics, SunMi or Next Order.

## License

[MIT](LICENSE)
