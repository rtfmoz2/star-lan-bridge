#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""star_probe: talk to a Star LAN printer (or star-lan-bridge) the way StarIO does.

Read-only by default: nothing here prints a receipt.

  star_probe.py discover [--broadcast 192.168.1.255] [--timeout 3]
      Send the 28-byte Star Discovery query to UDP 22222 and decode every reply.

  star_probe.py discover --host 192.168.1.134
      Unicast query to one printer (this is how some POS apps test "connected").

  star_probe.py status --host 192.168.1.134 [--count 3]
      Poll TCP 9101 like StarIO: send 0x32 + 50 NULs, read two 11-byte ASB frames.

  star_probe.py etb --host 192.168.1.134
      Connect to TCP 9100, clear the ETB counter, send one ETB and report the
      counter before/after from the ASB frames. Prints nothing (no image data).

Works on macOS and Linux with the Python standard library (3.8+).
"""
from __future__ import annotations

import argparse
import socket
import sys
import time

QUERY = (b"STR_BCAST" + b"\0" * 7      # 0x00 magic, NUL-padded to 16 bytes
         + b"RQ1.0.0\0"                # 0x10 request version
         + b"\x00\x1c"                 # 0x18 total length = 28, big-endian
         + b"\x64\x31")                # 0x1A request id (any value; not echoed)
STATUS_QUERY = b"\x32" + b"\0" * 50    # 51-byte 9101 poll, as sent by StarIO


def cstr(b: bytes) -> str:
    """Decode a NUL-padded ASCII field."""
    return b.split(b"\0", 1)[0].decode("ascii", "replace")


def decode_discovery(d: bytes) -> dict:
    """Pull the interesting fields out of a 302-byte RS1.0.1 reply."""
    return {
        "length": len(d),
        "version": cstr(d[0x10:0x18]),
        "short_model": cstr(d[0x23:0x34]),
        "firmware": cstr(d[0x36:0x3A]),
        "mac": d[0x4E:0x54].hex(":"),
        "ip": socket.inet_ntoa(d[0x58:0x5C]),
        "addressing": cstr(d[0x5C:0x60]),
        "netmask": socket.inet_ntoa(d[0x6C:0x70]),
        "gateway": socket.inet_ntoa(d[0x70:0x74]),
        "manufacturer": cstr(d[0x8C:0x90]),
        "command_set": cstr(d[0xAC:0xB0]),
        "model": cstr(d[0xCC:0x10C]),
        "device_class": cstr(d[0x10C:0x114]),
    }


def asb_fields(f: bytes) -> str:
    """One-line summary of an ASB frame. Byte 7 carries the ETB counter in bits 1-5."""
    if len(f) < 9:
        return f.hex(" ")
    etb = (f[7] >> 1) & 0x1F
    return f"{f.hex(' ')}  (iface={'LAN' if f[1] & 0x80 else 'USB/BT'}, etb_counter={etb})"


def cmd_discover(a):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    s.settimeout(0.5)
    target = a.host or a.broadcast
    s.sendto(QUERY, (target, 22222))
    seen, end = set(), time.time() + a.timeout
    while time.time() < end:
        try:
            d, addr = s.recvfrom(2048)
        except socket.timeout:
            continue
        if addr in seen:
            continue
        seen.add(addr)
        info = decode_discovery(d)
        print(f"reply from {addr[0]}:{addr[1]}")
        for k, v in info.items():
            print(f"  {k:13} {v}")
    if not seen:
        print(f"no reply from {target} within {a.timeout}s", file=sys.stderr)
        return 1
    return 0


def cmd_status(a):
    for i in range(a.count):
        t = time.time()
        c = socket.create_connection((a.host, 9101), timeout=3)
        c.sendall(STATUS_QUERY)
        r = b""
        c.settimeout(2)
        try:
            while True:
                x = c.recv(64)
                if not x:
                    break
                r += x
        except socket.timeout:
            pass
        c.close()
        print(f"poll {i + 1}: {len(r)} bytes in {1000 * (time.time() - t):.0f} ms")
        for j in range(0, len(r), 11):
            print("   ", asb_fields(r[j:j + 11]))
        time.sleep(0.2)
    return 0


def read_frames(sock: socket.socket, wait: float) -> list[bytes]:
    out, end = b"", time.time() + wait
    sock.settimeout(0.2)
    while time.time() < end:
        try:
            x = sock.recv(256)
            if not x:
                break
            out += x
        except socket.timeout:
            pass
    return [out[i:i + 11] for i in range(0, len(out), 11)]


def cmd_etb(a):
    c = socket.create_connection((a.host, 9100), timeout=5)
    for f in read_frames(c, 1.0):
        print("on connect:  ", asb_fields(f))
    c.sendall(b"\x1b\x1e\x45\x00")         # ESC RS E 0: clear ETB counter
    for f in read_frames(c, 1.0):
        print("after clear: ", asb_fields(f))
    c.sendall(b"\x17")                     # ETB: printer increments the counter when it gets here
    for f in read_frames(c, 1.5):
        print("after ETB:   ", asb_fields(f))
    c.close()
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("discover", help="Star Discovery query on UDP 22222")
    d.add_argument("--broadcast", default="255.255.255.255", help="broadcast address to query")
    d.add_argument("--host", help="unicast the query to this address instead")
    d.add_argument("--timeout", type=float, default=3.0)
    s = sub.add_parser("status", help="poll TCP 9101")
    s.add_argument("--host", required=True)
    s.add_argument("--count", type=int, default=3)
    e = sub.add_parser("etb", help="round-trip the ETB counter over TCP 9100 (prints nothing)")
    e.add_argument("--host", required=True)
    a = ap.parse_args()
    return {"discover": cmd_discover, "status": cmd_status, "etb": cmd_etb}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
