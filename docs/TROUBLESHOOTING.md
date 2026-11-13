# Troubleshooting

Start with the bridge log. It records every discovery reply, print job and link change:

```bash
journalctl -u star-lan-bridge -f
```

Every line names the printer it's about, e.g. `[TSP143-2] 9100: connection from
192.168.1.141:45328` (in single-printer mode the name is `printer`).

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
`discovery: answered <pos-ip>`. If the Pi's IP has changed, the app is querying
the old address: add a DHCP reservation.

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
* Is its address on the host? `ip -4 addr show wlan0` must list it. The bridge
  logs `can't bind <address>:9100 yet ... retrying every 5s` until it appears.
* From another machine, `python3 tools/star_probe.py discover` should get one
  reply per printer, and `discover --host <address>` only that printer.
* Did something else take the address? Exclude each printer's address from the
  router's DHCP pool. If another device answers there, the POS reaches it instead.

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
