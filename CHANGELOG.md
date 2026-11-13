# Changelog

## 0.4.0 (2026-11-14)

Hardening for commercial deployment (docs/SECURITY.md).

* **Bridge:** only devices on the host's own subnets (and loopback) may print,
  poll or discover. 9100 connections close after 60 s idle or 5 minutes, and
  jobs over 8 MB are cut off. 9101 queries are handled without blocking and
  rate-limited. Fresh printer status is requested at most every 2 s.
  Discovery is rate-limited, never replies to spoofed or outside sources, and
  ignores oversized packets. Logs of refusals and floods are throttled. The
  printer receive buffer is capped.
* **Fuzz tests** (`tests/test_fuzz.py`) run the real bridge loop against a fake
  printer with random jobs, idle, oversized and reset connections, garbage
  9101 queries, silent-client floods, printer garbage and junk UDP. They found,
  and this release fixes, two ways to stall a printer: silent 9101 clients and
  repeated status requests to a printer that doesn't answer.
* **systemd sandbox:** the bridge runs with no capabilities, RFCOMM devices
  only, a system-call filter and no route to the internet (exposure score
  1.8). star-admin keeps only three network capabilities (2.4).
* **`scripts/harden.sh`:** nftables default-deny firewall for venue-LAN-only
  access (with a 3-minute automatic rollback until confirmed), CUPS local only,
  SSH key-only with no root or forwarding, Bluetooth not discoverable or
  pairable. Pending Debian security updates are installed once, then automatic
  updates are turned off (set-and-forget); `--auto-updates` keeps them on,
  security archive only.
* **Logs and storage:** the journal is kept across reboots (it was RAM-only)
  with hard limits: 100 MB, one month, 2 GB always free, and a per-service
  rate limit. apt no longer keeps downloaded packages (195 MB freed); CUPS
  logs warnings only, up to 1 MB. The admin page no longer logs routine page
  loads and refreshes.
* **docs:** new LOGS.md: where each part logs, everyday journalctl recipes,
  logs from before a power cut, copying logs to a laptop, and what the
  important lines mean.
* **docs:** corrected the DHCP reservation advice. A fixed IP for the Pi's MAC
  on UniFi and other MAC-based routers can capture every printer lease, so
  with several printers, leave the leases as plain DHCP (ADMIN.md, "Router
  reservations").
* **Admin page printer cards:** the top right shows the printer's connection
  (connected, connecting, not connected); the Address row shows its IP; the
  last row, Status, shows print readiness (Ready, Out of paper, Switched off or
  out of Bluetooth range) instead of raw status bytes, which remain as a
  tooltip. The separate Bluetooth row and address explanation are gone.
* **`/sshctl`:** an admin page not linked from the main page (sign-in
  required) that turns SSH logins off or on, now and at boot, with a status
  badge (Accepted, Denied, or pending while it changes) and every change
  logged. Recovery without the page: an `ssh` file on the boot partition.
  See SECURITY.md.
* **Admin page:** never shows cached data. Responses can't be cached, the page
  refreshes when it comes back into view, and it shows when it last updated
  (or a warning if the Pi can't be reached). Routes ignore query strings.

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
