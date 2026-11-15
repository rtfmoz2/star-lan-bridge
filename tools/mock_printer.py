#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""mock_printer: a stand-in for a Bluetooth TSP100III, for testing without printers.

Creates a pseudo-terminal that behaves like the printer's /dev/rfcommN as far
as the bridge can tell, and links it at --link. Point a printer's `device` in
the bridge config at that link, and the POS, CUPS and tools/star_probe.py can
discover it, poll its status and print to it. Nothing is printed: each job is
saved to --jobs as a .bin file and logged with its size.

It answers the commands the bridge relies on:
  ESC ACK SOH (1B 06 01)   status request: one ASB frame
  ESC RS a n  (1B 1E 61 n) ASB on: one ASB frame
  ESC RS E n  (1B 1E 45 n) clear the ETB counter, then one ASB frame
  ETB         (17)         at the end of a burst of data: count it, one ASB frame
ASB frames are the 9-byte Bluetooth/USB shape (23 06 ...), which the bridge
converts to the LAN shape.

  kill -USR1 <pid>   toggle "out of paper"
  kill -USR2 <pid>   toggle "cover open"

Example (two printers on a test device, no Bluetooth needed):
  python3 tools/mock_printer.py --link /run/mock-printer/TSP143 &
  python3 tools/mock_printer.py --link /run/mock-printer/TSP143-2 &

Linux and macOS, Python standard library only.
"""
from __future__ import annotations

import argparse
import logging
import os
import pty
import select
import signal
import sys
import time
import tty

STATUS_REQUEST = b"\x1b\x06\x01"
ASB_ON = b"\x1b\x1e\x61"
CLEAR_ETB = b"\x1b\x1e\x45"
ETB = 0x17
JOB_GAP = 1.5      # seconds of silence that end a job
ETB_SETTLE = 0.1   # an ETB counts only if nothing follows it this quickly

log = logging.getLogger("mock")


class MockPrinter:
    def __init__(self, link: str, jobs: str):
        self.master, self.slave = pty.openpty()
        tty.setraw(self.master)
        self.path = os.ttyname(self.slave)
        self.link, self.jobs = link, jobs
        self.etb = 0
        self.paper_out = False
        self.cover_open = False
        self.job = bytearray()
        self.job_count = 0
        self.last_rx = 0.0
        self.tail = b""          # last bytes seen, so commands split across reads still match
        self.etb_at: float | None = None
        self.write = lambda data: os.write(self.master, data)
        os.makedirs(os.path.dirname(os.path.abspath(link)), exist_ok=True)
        os.makedirs(jobs, exist_ok=True)
        try:
            os.unlink(link)
        except FileNotFoundError:
            pass
        os.symlink(self.path, link)

    def frame(self) -> bytes:
        """A 9-byte Bluetooth/USB ASB frame with the current state."""
        f = bytearray(b"\x23\x06\x00\x00\x00\x00\x00\x00\x00")
        if self.cover_open:
            f[2] |= 0x20 | 0x08          # cover open, offline
        if self.paper_out:
            f[5] |= 0x08                 # paper empty
            f[2] |= 0x08                 # offline
        f[7] = (self.etb & 0x1F) << 1
        return bytes(f)

    def send_status(self, why: str):
        self.write(self.frame())
        log.debug("status (%s): %s", why, self.frame().hex(" "))

    def handle(self, data: bytes):
        now = time.monotonic()
        if self.etb_at is not None:
            self.etb_at = None           # more data straight after the ETB: it was raster data
        self.job += data
        self.last_rx = now
        scan = self.tail + data
        for cmd, why in ((STATUS_REQUEST, "request"), (ASB_ON, "ASB on")):
            if cmd in scan:
                self.send_status(why)
        i = scan.find(CLEAR_ETB)
        if i >= 0 and i + 3 < len(scan):
            self.etb = 0
            self.send_status("ETB cleared")
        if data and data[-1] == ETB:
            self.etb_at = now
        self.tail = scan[-4:]

    def tick(self):
        now = time.monotonic()
        if self.etb_at is not None and now - self.etb_at >= ETB_SETTLE:
            self.etb_at = None
            self.etb = (self.etb + 1) & 0x1F
            self.send_status(f"ETB {self.etb}")
        if self.job and now - self.last_rx >= JOB_GAP:
            self.finish_job()

    def finish_job(self):
        data = bytes(self.job)
        self.job.clear()
        if len(data) <= 16:
            return                       # status traffic only, not a job
        self.job_count += 1
        name = os.path.join(self.jobs, f"job-{time.strftime('%Y%m%d-%H%M%S')}-{self.job_count}.bin")
        with open(name, "wb") as f:
            f.write(data)
        log.info("job %d: %d bytes -> %s", self.job_count, len(data), name)

    def run(self):
        log.info("mock printer on %s, linked at %s; jobs go to %s", self.path, self.link, self.jobs)
        while True:
            r, _, _ = select.select([self.master], [], [], 0.05)
            if r:
                try:
                    data = os.read(self.master, 65536)
                except OSError:
                    data = b""
                if data:
                    self.handle(data)
            self.tick()

    def close(self):
        try:
            if os.readlink(self.link) == self.path:
                os.unlink(self.link)
        except OSError:
            pass


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--link", required=True, help="where to link the device, e.g. /run/mock-printer/TSP143")
    ap.add_argument("--jobs", help="directory for received jobs (default: <link>-jobs)")
    ap.add_argument("-v", "--verbose", action="store_true", help="log every status frame")
    a = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format=f"%(asctime)s {os.path.basename(a.link)}: %(message)s", datefmt="%H:%M:%S")
    m = MockPrinter(a.link, a.jobs or a.link + "-jobs")

    def toggle(attr, label):
        def handler(*_):
            setattr(m, attr, not getattr(m, attr))
            log.info("%s: %s", label, "on" if getattr(m, attr) else "off")
            m.send_status(label)
        return handler

    signal.signal(signal.SIGUSR1, toggle("paper_out", "out of paper"))
    signal.signal(signal.SIGUSR2, toggle("cover_open", "cover open"))
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    try:
        m.run()
    except KeyboardInterrupt:
        pass
    finally:
        m.close()


if __name__ == "__main__":
    main()
