# Troubleshooting

Start with the bridge log. It records every discovery reply, print job and link
change. [LOGS.md](LOGS.md) covers every log on the Pi and how to read it.

```bash
journalctl -u star-lan-bridge -f
```

Logs survive reboots and power cuts (capped at 100 MB and one month). After a
power cut, `journalctl -u star-lan-bridge -b -1` shows the boot before it.

Every line names the printer it's about, e.g. `[TSP143-2] 9100: connection from
<pos-address>:45328` (in single-printer mode the name is `printer`).

## The Pi doesn't join the venue's Wi-Fi

**Symptom:** at a new venue the Pi never comes up on the network, even though
the network was saved on the admin page and the name looks right.

* **The name must match exactly**, including spaces, capitals and
  apostrophes. Staff often write it down slightly differently from what the
  router broadcasts (`Cafe123` against `Cafe 123`). Copy it from the router's
  label or settings, not from a note.
* **The password must match exactly** too. A typo looks the same as a wrong
  network: the Pi just never joins.
* **Join from the scan list.** On the admin page, tap the network in the scan
  list instead of typing its name: the page copies the name exactly. If the
  Pi isn't on any network, turn on the phone hotspot you saved as a second
  network ([ADMIN.md, Moving the Pi to a new venue](ADMIN.md#moving-the-pi-to-a-new-venue)),
  open the admin page from the phone, and join the venue network from there.
* **See why it failed.** The attempt is kept in the journal, even across a
  reboot:

  ```bash
  sudo journalctl -u NetworkManager -u wpa_supplicant -b -1 --no-pager | grep -iE 'assoc|auth|handshake|reason|fail|secrets|not found'
  ```

  `4-way handshake failed` or `secrets were required` means the password was
  wrong; `No network with SSID … found` means the name didn't match. Drop
  `-b -1` if the Pi hasn't rebooted since.

### Collecting everything for a closer look

`tools/collect-wifi-logs.sh` gathers all of it into one archive: every boot's
NetworkManager, wpa_supplicant and star-admin logs, a summary of the lines that
usually explain a failed join, the saved networks (names also in hex, which
shows hidden characters), what the Pi can see (channel, band, security), and
the Wi-Fi country. Passwords are removed. Run it **before** fixing anything,
because joining from the scan list replaces the evidence:

```bash
./tools/collect-wifi-logs.sh                 # writes /tmp/wifi-logs-<date>.tar.gz
scp <user>@<pi-address>:/tmp/wifi-logs-*.tar.gz .     # from the laptop
```

To catch the next failure in more detail, turn on NetworkManager's detailed
logging first (it lasts across reboots, so turn it off afterwards):

```bash
./tools/collect-wifi-logs.sh --debug-on
./tools/collect-wifi-logs.sh --debug-off
```

Logs of earlier boots exist only if the journal is kept on the card
(`scripts/harden.sh` sets that up); otherwise only the current boot is there.

## Neither printer works and the bridge keeps restarting

**Symptom:** the admin page works, but the POS finds no printers.
`systemctl status star-lan-bridge` shows `activating (auto-restart)`, and
`systemctl show star-lan-bridge -p NRestarts` keeps climbing.

**Cause:** in versions before 0.5.0 the bridge exited when two printers had
the same address, and systemd restarted it straight into the same error:

```
ValueError: [printer:TSP143-2] address <pi-address> is used by another printer
```

This happened at a venue whose router hands out addresses by MAC and ignores
the DHCP client ID, so printer 2's "own" lease was the Pi's own address.

**Fix:** update to 0.5.0 or later, where a clash only skips the printer
involved and `address = dhcp` falls back to the next available address by
itself. Better still, set `address = next` for every extra printer (see
[ADMIN.md, How printer addresses work](ADMIN.md#how-printer-addresses-work)).
No fixed address is needed.

## The admin page says another device is using a printer's address

**Symptom:** under a printer, the **Printers** section says *Another device is
also using …* and shows a **Find another address** button. Printing to that
printer may fail or go missing, because two devices answer on one address.

**Cause:** with `address = next`, the printer's address is one the router
doesn't know about, and the router (or someone setting a fixed address) gave
it to another device too. The printer never moves by itself, because the POS
stores its address. star-admin logs it every time it notices:

```
WARNING next: another device is also using <address>; TSP143-2 keeps it. Give that device another address, or reserve <address> in the router
```

**Fix**, either:

* **Move the other device** (give it another address, or restart it so it
  asks the router again). The warning clears within two minutes and nothing
  changes in the POS.
* **Press Find another address** under the printer, then press it again to
  confirm. The printer takes the next free address within a few seconds and
  the bridge restarts on it. Then delete the printer in the POS and add it
  again at the new address (see the next section).

To stop it happening again, end the router's DHCP range below the printers'
addresses, or set `next_from` above it (see
[ADMIN.md, `next`](ADMIN.md#next-the-next-free-address)).

## A printer moved to a new address and the POS can't print to it

**Symptom:** printing to one printer fails after it was working. The admin
page shows it on a different address from the one saved in the POS.

**Cause:** with `address = next`, a printer moves only when someone presses
**Find another address** on the admin page, or after a power cut if another
device took its address while the Pi was off. star-admin logs it:

```bash
journalctl -u star-admin | grep "next:"
```

```
WARNING next: moving TSP143-2 off <old-address>, as asked on the admin page; select it again in the POS
INFO next: TSP143-2 is using <new-address>/24 on wlan0
```

That normally needs the Pi to be off long enough for the router to hand the
address to something else, or a power cut where everything restarts at once.

**Fix:** the printer answers the POS's printer search at its new address
straight away. Next Order POS keeps the old address, though: **delete the
printer in the POS and add it again** from the search. The new address is
remembered, so the printer stays there.

## The POS prints two or more copies of every receipt

**Symptom:** each job takes about 30 s, then the same receipt prints again.
The log shows repeated `[NAME] 9100: connection from …` lines about 33 s apart.

**Cause:** the client disabled ASB (`ESC RS a 0`) and never saw the ETB
counter rise (see [PROTOCOL.md §4.3](PROTOCOL.md#43-why-esc-rs-a-0-is-stripped)).
The bridge strips that command. If you see this anyway, check that the log's
"finished" line reports `1 ASB-disable cmd(s) stripped` and a final status
with a non-zero byte 7, for example `23 86 02 00 00 00 00 02 00 00 00`.

## The POS says "not connected" but prints fine

Some apps (Next Order POS 6.0.0) check the connection with a unicast discovery
query to the saved IP, and don't make a TCP connection between jobs. A failed
earlier job can leave the label stuck until the app checks again. Open the
printer settings or search again, then confirm the log shows
`discovery: answered <pos-ip>`. If the printer's address has changed, the app is
querying the old address: delete the printer in the POS and add it again (see
[A printer moved to a new address](#a-printer-moved-to-a-new-address-and-the-pos-cant-print-to-it)).
Printers on `address = next` keep their address even if the Pi's own DHCP
address changes.

## Printing through CUPS takes ~50 seconds

`gstoraster` asks `colord` for an ICC profile. On headless Raspberry Pi OS,
`colord` exits as soon as it starts, so each request waits for a 25-second
D-Bus timeout, twice per job. Look for this in `/var/log/cups/error_log` with
debug logging on (`sudo cupsctl --debug-logging`):

```
Failed to activate service 'org.freedesktop.ColorManager': timed out (service_start_timeout=25000ms)
```

Fix: `sudo systemctl mask colord && sudo systemctl restart cups`. Undo with `unmask`.

## `printer: open /dev/rfcomm0 failed`

* Is the printer on and in range (about 10 m)?
* Is another device holding its single Bluetooth connection? Turn Bluetooth
  off on any phone or tablet that was paired with it.
* Is the binding there? `rfcomm` should list `rfcomm0: <MAC> channel 1`. If not,
  run `sudo systemctl restart rfcomm-printer`.
* Is it paired and trusted? `bluetoothctl info <MAC>` should show `Paired: yes`
  and `Trusted: yes`.

The bridge retries every 10 s by itself.

## No discovery replies

* Is the client on the same subnet or VLAN? UDP 22222 broadcasts aren't routed,
  and some Wi-Fi networks isolate clients ("AP isolation" or "client isolation"
  on UniFi).
* Run `sudo ss -lunp | grep 22222`: the bridge should own the port.
* Run `python3 tools/star_probe.py discover --host <pi-ip>` from another machine
  to test without broadcast.

## The first receipt after a long idle is slow

The bridge keeps the Bluetooth link open, so this is normally under a second.
If the printer powered down its radio, the first job waits for a reconnect
(a few seconds). The log shows `printer: link closed` and `connected` when
this happens.

## Phones can't connect to the printer directly any more

That's expected: the printer accepts one Bluetooth connection and the bridge
holds it. Print through the Pi instead, over IPP/AirPrint or Star LAN.

## Pairing says "Paired: yes" and then "Paired: no"

The adapter isn't pairable, so the bond isn't stored. Turn pairing on in the
same `bluetoothctl` session as the `pair` command:

```bash
{ echo "pairable on"; sleep 1; echo "pair <MAC>"; sleep 15; echo "trust <MAC>"; sleep 3; echo quit; } | bluetoothctl
bluetoothctl info <MAC> | grep -E "Paired|Bonded|Trusted"     # all three: yes
```

## A second printer doesn't show up in the POS

* Does the bridge know about it? At start it logs one
  `[NAME] bridge: listening on <address>, <local> ...` line per printer.
  If only one appears, check `/etc/star-lan-bridge.conf`.
* Does it have an address? `cat /run/star-lan-bridge/lease-<NAME>` should
  show `ip=...`; `journalctl -t star-dhcp` shows each DHCP lease and
  `journalctl -u star-admin | grep next:` each `next` address. With none, check
  that `star-admin` is running (`systemctl status star-admin`). Until it has
  one, the bridge logs `no address yet` and skips that printer; it restarts by
  itself when the address appears.
* Does the log say `address … is already used by printer …`? Two printers
  ended up with the same address, so the bridge serves the first and skips the
  other. With `address = dhcp` this shouldn't happen any more (a router that
  ignores the client ID makes the printer use `next`); check for a fixed
  address in the config that matches another printer's.
* Does the page say the router gives every printer the Pi's own address? It
  leases by MAC only, so the printer uses `next` instead (from `.200` up). That
  works; set `address = next` to skip the DHCP attempt.
* From another machine, `python3 tools/star_probe.py discover` should get one
  reply per printer, and `discover --host <address>` only that printer.
* Has the address changed? After a long power-off or on a new network, a
  printer may get a different address. The admin page shows the current one
  under the interface, for example `<address>/24 TSP143-2 (next available)`.
  Delete the printer in the POS and add it again from the search.
* Is there a fixed IP for the Pi's MAC on the router? On UniFi and other
  routers that reserve by MAC, it can apply to printer 2's lease too, because
  the same MAC is used. Remove it and restart star-admin
  (`sudo systemctl restart star-admin`). See
  [ADMIN.md, Router reservations](ADMIN.md#router-reservations).

## The POS reports the printer is out of paper (or every job "succeeds" but nothing prints)

The bridge passes the printer's own status through unchanged. A printer with
no paper reports `23 86 08 00 00 08 00 …`
(see [PROTOCOL.md §3.3](PROTOCOL.md#33-fields-the-bridge-depends-on)).
Check what it's reporting without printing anything:

```bash
python3 tools/star_probe.py status --host <printer-ip>
```

Load paper and close the cover. A healthy idle printer reports
`23 86 00 00 00 00 …`.

## Mock printers or the test bridge stop after logging out

When testing with `tools/mock_printer.py` ([TESTING.md](TESTING.md)),
processes started with `&` in an SSH session can stop when that session ends,
and they never come back after a reboot. The POS and `star_probe.py` then get
`Connection refused`. Run them as temporary systemd units instead, which
survive logouts (but still not reboots):

```bash
sudo systemd-run --unit=mock-TSP143 --uid=$USER python3 ~/star-lan-bridge/tools/mock_printer.py --link ~/mock/TSP143
sudo systemd-run --unit=test-bridge --uid=$USER -p Restart=always \
    python3 ~/star-lan-bridge/star_lan_bridge.py -c /etc/star-lan-bridge.conf
```

With the bridge stopped, the admin page shows each printer as
`not connected – No answer from the bridge`.
