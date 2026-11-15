# Setting up a Raspberry Pi as a Star LAN bridge

These are the steps used to build the reference setup: a Raspberry Pi 4 Model B
running Raspberry Pi OS Trixie Lite (64-bit), a Bluetooth Star TSP100III (model
number TSP143IIIBI; see [README, The printer](../README.md#the-printer-star-tsp100iii)), and
a SunMi D3 Pro running Next Order POS. Any Debian-based host with classic
Bluetooth and Python 3.8+ should work the same way.

Commands marked **Device** run on the device; **client** means another machine on the
same network.

## 1. Flash and reach the device

Use Raspberry Pi Imager → Raspberry Pi OS Lite (64-bit). In *OS customisation*
set a username, Wi-Fi network and country, and *Enable SSH → public-key
authentication only* with your public key. Then find the device:

```bash
# client (macOS shown): fill the ARP table, then look for Raspberry Pi MAC prefixes
NET=192.168.0   # the first three numbers of your network's addresses (example)
for i in $(seq 1 254); do ping -c1 -W 300 $NET.$i >/dev/null 2>&1 & done; wait
arp -an | grep -iE 'b8:27:eb|dc:a6:32|e4:5f:01|d8:3a:dd|2c:cf:67|28:cd:c1|88:a2:9e'
```

POS apps store the printer by IP address, so keep the device's address stable. With
**one printer per device**, give the device a DHCP reservation on the router. With
**several printers**, don't reserve the device's MAC on routers like UniFi; see
[ADMIN.md, Router reservations](ADMIN.md#router-reservations).

## 2. Wi-Fi (if not set in Imager)

```bash
# Device
sudo nmcli radio wifi on
nmcli dev wifi list
sudo nmcli --ask dev wifi connect "<SSID>" ifname wlan0   # prompts for the passphrase
```

## 3. Pair the printer

The TSP100III Bluetooth needs no button press: once it's switched on (blue LED
lit), it accepts pairing and shows up as **`TSP100-XXXXX`**, where the last part
is unique to each printer. The only button on the back, **RST**, resets the
printer; it isn't used for pairing. Disconnect any phone or tablet that's paired
to it first, because it accepts a single connection.

After pairing it with the device, Star recommends turning pairing off so no other
device can pair with the printer: in Star's **TSP100III Utility** app, open
**Setting → Bluetooth Settings**, set **New Pairing Permission** to **OFF** and tap
**Apply**. The device stays paired. To pair a new device later, set it back to ON.

```bash
# Device
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
# Device
sudo sdptool browse $M | grep -A8 -i "Serial Port" | grep -i Channel
```

## 4. Install the bridge

```bash
# Device
git clone https://github.com/rtfmoz2/star-lan-bridge.git
cd star-lan-bridge
sudo ./scripts/install.sh --mac $M --channel 1
```

Check it:

```bash
# Device
systemctl is-active rfcomm-printer star-lan-bridge
sudo ss -ltnup | grep -E '9100|9101|22222'
journalctl -u star-lan-bridge -f
```

```bash
# client: nothing below prints a receipt
python3 tools/star_probe.py discover
python3 tools/star_probe.py status --host <printer-address>
python3 tools/star_probe.py etb --host <printer-address>
```

Expected `discover` output includes `model TSP143IIILAN (STR_T-001)` and the
device's IP; `etb` shows the counter going from 0 to 1.

With no config file the bridge serves this one printer on all of the device's
addresses. For a second printer, see [§7](#7-several-printers). To join Wi-Fi networks and see the
addresses from a browser, see [ADMIN.md](ADMIN.md).

## 5. Optional: CUPS for laptops and phones

Download **Star CUPS Driver for Linux** (V3.16.0 used here) from Star's
[TSP100III support page](https://starmicronics.com/support/products/tsp100iii-support-page/).
It needs a Star account. Copy the `.tar` to the device, then:

```bash
# Device
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

In the POS app, search for **Star LAN** printers, or add one by IP using the device's
address. In Next Order POS 6.0.0, the printer showed as *connected* and printed
with the app's TSP100III model set to either the W (Wi-Fi) or the LAN variant.

## 7. Several printers

Each printer needs its own LAN address, because Star clients identify printers by
IP and the ports (22222, 9100, 9101) are fixed. No address is fixed in the
config, so the same configuration works on any venue network:

* **One printer** uses the device's own DHCP address (`address = auto`).
* **Each extra printer** uses the next free address after the device's
  (`address = next`): with the device on `.10`, `.11` if no device answers on it,
  otherwise `.12`, and so on. `next_from` in the `[network]` section starts
  the search elsewhere, such as `.200` ([CONFIG.md](CONFIG.md#next_from)). The `star-admin` service
  ([ADMIN.md](ADMIN.md)) checks, claims and remembers it, and the printer
  never moves by itself. This works with any router.
* Or **its own DHCP lease** (`address = dhcp`), on routers that honour a
  separate DHCP client ID per printer, UniFi included. On routers that don't,
  the printer falls back to `next` by itself.

Steps:

1. **Pair the second printer and bind it to `/dev/rfcomm1`.** If pairing shows
   `Paired: yes` and then `Paired: no` right away, the adapter isn't pairable:
   run `pairable on` in the same `bluetoothctl` session before `pair`.

   ```bash
   # device
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
   `[network]` section with `address = next`, which applies to every printer,
   and one `[printer:NAME]` section per printer, each with its own `device`
   and `local`, and a distinct `mac` for every printer after the first.

3. **Install star-admin**, which keeps the extra addresses (and gives you the web page):

   ```bash
   # device
   sudo ./scripts/install-admin.sh
   sudo python3 /opt/star-lan-bridge/star_admin.py --set-password
   cat /run/star-lan-bridge/lease-TSP143-2        # ip=... once it has an address
   ```

4. **Restart the bridge once.** After that it restarts by itself whenever an
   address changes. Then point each CUPS queue at its printer's local address:

   ```bash
   # device
   sudo systemctl restart star-lan-bridge
   journalctl -u star-lan-bridge -n 10 -o cat     # one "listening on" line per printer
   sudo lpadmin -p TSP143-2 -E -v socket://127.0.0.2:9100 -m star/tsp143.ppd -o printer-is-shared=true
   ```

5. **Check from another machine.** A broadcast gets one reply per printer:

   ```bash
   python3 tools/star_probe.py discover --broadcast <broadcast-address>   # e.g. 192.168.0.255 on a 192.168.0.x network
   python3 tools/star_probe.py status --host <printer-2-address>
   ```

6. **Leave the addresses alone on the router.** They stay stable by
   themselves. With `dhcp`, don't add a fixed IP for the device's MAC on UniFi or
   other routers that reserve by MAC: it can apply to every printer's lease,
   because they all use that MAC. See
   [ADMIN.md, Router reservations](ADMIN.md#router-reservations).

To try all of this on a spare device without printers, see [TESTING.md](TESTING.md).

## Uninstall

```bash
# Device: put CUPS back on the serial device, keep the rfcomm binding for it
sudo ./scripts/uninstall.sh --cups-queue TSP143 --keep-rfcomm
```
