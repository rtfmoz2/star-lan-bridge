# Changelog

## 0.3.0 (2026-11-14)

* **Web admin page (`star_admin.py`, `star-admin.service`).** At
  `http://<hostname>.local`, password-protected: shows the Pi's network and
  addresses and each printer's address, Bluetooth link and status (ready, out
  of paper, switched off); scans for Wi-Fi and joins a network now or saves it
  for later; forgets saved networks. See docs/ADMIN.md.
* **DHCP-only addressing.** `address = auto` uses the host's own DHCP address;
  `address = dhcp` gives a printer its own DHCP lease, requested by star-admin
  with a per-printer client ID and renewed automatically. No fixed addresses
  are needed, so the same config works on any network.
* The bridge restarts itself when a printer's address changes (new lease, new
  network), and skips a printer whose address isn't known yet instead of failing.
* **Fix: a switched-off printer no longer freezes the bridge.** Opening an RFCOMM
  device blocked until the printer answered, forever if it was off, which stopped
  that printer's 9100/9101 service. Connections are now non-blocking with a
  20-second limit and 10-60 s back-off; while a printer is away, status replies
  carry the offline bit, and a job arriving during a reconnect waits up to 15 s.

## 0.2.0 (2026-11-13)

* **Several printers on one host.** `/etc/star-lan-bridge.conf` (see
  `examples/star-lan-bridge.conf`) defines one `[printer:NAME]` per printer,
  each with its own RFCOMM `device`, LAN `address`, optional loopback `local`
  address for CUPS and optional discovery `mac`. Broadcast discovery gets one
  reply per printer, each from its own address; a unicast query gets only the
  printer at that address. Each printer has its own 9100/9101 listeners and
  link. Without a config file the bridge behaves exactly as in 0.1.0.
* Log lines are prefixed with the printer's name, e.g. `[TSP143-2] 9100: ...`.
* Listeners wait for an address that isn't up yet (for example a secondary IP
  NetworkManager hasn't added at boot) instead of crash-looping.
* Fix: the `ESC RS a 0` filter could strip a lookalike sequence formed by the
  bytes on either side of a removed command when it arrived across reads.
  Chunked filtering now matches whole-stream filtering exactly.
* docs: SETUP.md §7 "Several printers"; PROTOCOL.md §2.4 multi-printer
  discovery and the observed paper-empty ASB pattern.
* docs: README covers several printers; TROUBLESHOOTING.md adds pairing that
  won't stick, a second printer that doesn't appear, and paper-out status;
  repository URLs filled in.

## 0.1.0 (2026-11-13)

First release.

* Star Discovery responder on UDP 22222, replying as a TSP143IIILAN with the
  live IP, MAC, netmask and gateway of the interface on the client's subnet.
* TCP 9100 bidirectional print relay to an RFCOMM TTY, converting USB/Bluetooth
  ASB frames (9 bytes) to the LAN shape (11 bytes).
* TCP 9101 status polling, answered from the printer's real latest status.
* `ESC RS a 0` is stripped from print data so the ETB counter keeps flowing.
  This fixes 30-second timeouts and duplicate receipts from StarIO clients.
* `tools/star_probe.py`: read-only discover / status / ETB test client.
* systemd units, an installer, an uninstaller and a CUPS helper script.
