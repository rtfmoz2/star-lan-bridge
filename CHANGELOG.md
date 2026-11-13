# Changelog

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
