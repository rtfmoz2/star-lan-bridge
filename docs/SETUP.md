# Setting up a Raspberry Pi as a Star LAN bridge

These are the steps used to build the reference setup: a Raspberry Pi 4 Model B
running Raspberry Pi OS Trixie Lite (64-bit), a Bluetooth Star TSP143IIIBI, and
a SunMi D3 Pro running Next Order POS. Any Debian-based host with classic
Bluetooth and Python 3.8+ should work the same way.

Commands marked **Pi** run on the Pi; **client** means another machine on the
same network.

## 1. Flash and reach the Pi

Use Raspberry Pi Imager → Raspberry Pi OS Lite (64-bit). In *OS customisation*
set a username, Wi-Fi network and country, and *Enable SSH → public-key
authentication only* with your public key. Then find the Pi:

```bash
# client (macOS shown): fill the ARP table, then look for Raspberry Pi MAC prefixes
for i in $(seq 1 254); do ping -c1 -W 300 192.168.1.$i >/dev/null 2>&1 & done; wait
arp -an | grep -iE 'b8:27:eb|dc:a6:32|e4:5f:01|d8:3a:dd|2c:cf:67|28:cd:c1|88:a2:9e'
```

POS apps store the printer by IP address, so keep the Pi's address stable. With
**one printer per Pi**, give the Pi a DHCP reservation on the router. With
**several printers**, don't reserve the Pi's MAC on routers like UniFi; see
[ADMIN.md, Router reservations](ADMIN.md#router-reservations).

## 2. Wi-Fi (if not set in Imager)

```bash
# Pi
sudo nmcli radio wifi on
nmcli dev wifi list
sudo nmcli --ask dev wifi connect "<SSID>" ifname wlan0   # prompts for the passphrase
```

## 3. Pair the printer

Hold **PAIR** on the back of the TSP143IIIBI for about 5 seconds, until the blue LED
flashes. Disconnect any phone or tablet that's paired to it, because it accepts a
single connection.

```bash
# Pi
sudo apt install -y bluez
bluetoothctl power on
bluetoothctl --timeout 30 scan bredr          # classic Bluetooth only
bluetoothctl devices | grep -iE 'tsp|star|00:11:62'
M=00:11:62:AA:BB:CC                           # your printer
{ echo "agent NoInputNoOutput"; echo default-agent; sleep 1; echo "pair $M"; sleep 12; echo "trust $M"; sleep 2; echo quit; } | bluetoothctl
bluetoothctl info $M | grep -E 'Paired|Trusted|Serial Port'
```

Find the SPP channel (usually 1):

```bash
# Pi
sudo sdptool browse $M | grep -A8 -i "Serial Port" | grep -i Channel
```

## 4. Install the bridge

```bash
# Pi
git clone https://github.com/rtfmoz2/star-lan-bridge.git
cd star-lan-bridge
sudo ./scripts/install.sh --mac $M --channel 1
```

Check it:

```bash
# Pi
systemctl is-active rfcomm-printer star-lan-bridge
sudo ss -ltnup | grep -E '9100|9101|22222'
journalctl -u star-lan-bridge -f
```

```bash
# client: nothing below prints a receipt
python3 tools/star_probe.py discover
python3 tools/star_probe.py status --host 192.168.1.134
python3 tools/star_probe.py etb --host 192.168.1.134
```

Expected `discover` output includes `model TSP143IIILAN (STR_T-001)` and the
Pi's IP; `etb` shows the counter going from 0 to 1.

With no config file the bridge serves this one printer on all of the Pi's
addresses. For a second printer, see [§7](#7-several-printers). To join Wi-Fi networks and see the
addresses from a browser, see [ADMIN.md](ADMIN.md).

## 5. Optional: CUPS for laptops and phones

Download **Star CUPS Driver for Linux** (V3.16.0 used here) from Star's
[TSP100III support page](https://starmicronics.com/support/products/tsp100iii-support-page/).
It needs a Star account. Copy the `.tar` to the Pi, then:

```bash
# Pi
sudo ./scripts/setup-cups.sh --driver ~/Star_CUPS_Driver-3.16.0_linux.tar --queue TSP143
echo "Hello" | lp -d TSP143
```

The script builds the driver from Star's bundled source (the binary in the
archive is x86_64), masks `colord` (see [TROUBLESHOOTING.md](TROUBLESHOOTING.md)),
shares the queue over IPP/AirPrint and points it at the bridge.

Adding the printer on clients:

* **macOS and iOS:** it appears as a Bonjour printer. If macOS asks for a driver,
  choose *Generic PostScript Printer*.
* **Windows:** add it by URL as `http://<pi-ip>:631/printers/TSP143` with a generic PostScript driver.

## 6. Point the POS at it

In the POS app, search for **Star LAN** printers, or add one by IP using the Pi's
address. In Next Order POS 6.0.0, the printer showed as *connected* and printed
with the app's TSP100III model set to either the W (Wi-Fi) or the LAN variant.

## 7. Several printers

Each printer needs its own LAN address, because Star clients identify printers by
IP and the ports (22222, 9100, 9101) are fixed. Addresses come from DHCP, so the
same configuration works on any venue network:

* **One printer** uses the Pi's own DHCP address (`address = auto`).
* **Each extra printer** gets its own DHCP lease (`address = dhcp`). The
  `star-admin` service ([ADMIN.md](ADMIN.md)) requests it with BusyBox `udhcpc`,
  using the Pi's MAC (Wi-Fi access points only accept that one) and a separate
  DHCP **client ID** per printer, which routers treat as a separate device.
  The client ID is fixed per Pi and printer, so the router keeps giving back the
  same address.

Steps:

1. **Pair the second printer and bind it to `/dev/rfcomm1`.** If pairing shows
   `Paired: yes` and then `Paired: no` right away, the adapter isn't pairable:
   run `pairable on` in the same `bluetoothctl` session before `pair`.

   ```bash
   # Pi
   M2=00:11:62:DD:EE:FF
   { echo "pairable on"; sleep 1; echo "pair $M2"; sleep 15; echo "trust $M2"; sleep 3; echo quit; } | bluetoothctl
   sed -e "s/@PRINTER_MAC@/$M2/g" -e "s/@CHANNEL@/1/g" \
       -e "s/rfcomm bind 0/rfcomm bind 1/" -e "s/rfcomm release 0/rfcomm release 1/" \
       -e "s#/dev/rfcomm0#/dev/rfcomm1#" \
       systemd/rfcomm-printer.service | sudo tee /etc/systemd/system/rfcomm-printer2.service
   sudo systemctl daemon-reload && sudo systemctl enable --now rfcomm-printer2
   ```

2. **Write `/etc/star-lan-bridge.conf`** from
   [examples/star-lan-bridge.conf](../examples/star-lan-bridge.conf): one
   `[printer:NAME]` section per printer, `address = auto` for the first and
   `address = dhcp` for the rest, each with its own `device` and `local`, and a
   distinct `mac` for every printer after the first.

3. **Install star-admin**, which keeps the extra leases (and gives you the web page):

   ```bash
   # Pi
   sudo ./scripts/install-admin.sh
   sudo python3 /opt/star-lan-bridge/star_admin.py --set-password
   cat /run/star-lan-bridge/lease-TSP143-2        # ip=... once the lease arrives
   ```

4. **Restart the bridge once.** After that it restarts by itself whenever an
   address changes. Then point each CUPS queue at its printer's local address:

   ```bash
   # Pi
   sudo systemctl restart star-lan-bridge
   journalctl -u star-lan-bridge -n 10 -o cat     # one "listening on" line per printer
   sudo lpadmin -p TSP143-2 -E -v socket://127.0.0.2:9100 -m star/tsp143.ppd -o printer-is-shared=true
   ```

5. **Check from another machine.** A broadcast gets one reply per printer:

   ```bash
   python3 tools/star_probe.py discover --broadcast 192.168.1.255
   python3 tools/star_probe.py status --host <printer-2-address>
   ```

6. **Leave the leases as plain DHCP.** They stay stable by themselves. Don't
   add a fixed IP for the Pi's MAC on UniFi or other routers that reserve by
   MAC: it can apply to every printer's lease, because they all use that MAC.
   See [ADMIN.md, Router reservations](ADMIN.md#router-reservations).

If a router hands out leases by MAC address only, every client ID gets the
Pi's own address. The page then shows printer 2 with the same address as
printer 1; that network can't run more than one printer per Pi.

## Uninstall

```bash
# Pi: put CUPS back on the serial device, keep the rfcomm binding for it
sudo ./scripts/uninstall.sh --cups-queue TSP143 --keep-rfcomm
```
