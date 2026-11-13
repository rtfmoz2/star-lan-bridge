#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""star-lan-bridge: make a Bluetooth Star TSP143IIIBI look like a Star LAN printer.

Star's POS SDKs (StarIO, StarIO10, StarPRNT) and the apps built on them find
network receipt printers with Star's own UDP discovery protocol and then talk
to them over two TCP ports. A Bluetooth-only printer has none of that. This
bridge runs on a small Linux box (a Raspberry Pi in practice) that is paired
with the printer, and answers on the network exactly like a TSP143IIILAN:

    UDP 22222  Star Discovery Protocol responder
               28-byte "STR_BCAST" query  ->  302-byte identity reply
    TCP 9100   raw print channel, bidirectional
               client bytes -> printer;  printer ASB status frames -> client
    TCP 9101   status polling channel
               client sends a 51-byte query, gets two 11-byte ASB frames, closed

The printer is reached over Bluetooth Serial Port Profile (SPP) through an
RFCOMM TTY, normally /dev/rfcomm0, which `rfcomm bind` creates (see
systemd/rfcomm-printer.service). The bridge is meant to be the *only* process
that opens that TTY. Point any CUPS queue at socket://127.0.0.1:9100 so CUPS
jobs and POS jobs queue behind each other instead of fighting over the single
Bluetooth connection the printer accepts.

Design notes
------------
* Single-threaded select() loop for TCP + printer I/O, plus one daemon thread
  for UDP discovery (discovery must never wait behind a slow Bluetooth write).
* One print client at a time. While a 9100 client is connected the listening
  socket is left out of select(), so later clients wait in the kernel backlog.
* Status is never synthesised. POS apps compare the ETB counter they see on
  9100/9101 with what they expect; a fake counter makes them think the job
  failed and resend it. The bridge only relays the printer's real ASB frames.
* ASB (Automatic Status Back) is kept on permanently. Clients often send
  ESC RS a 0 (disable ASB) and then poll 9101 for the ETB counter, which only
  updates while ASB is on. The bridge strips that one command from the stream.
  See docs/PROTOCOL.md, "Why ESC RS a 0 is stripped".

Protocol details (discovery template, ASB framing, 9101 behaviour) follow the
reverse-engineering notes published in github.com/gnaunited/tsp143-bridge
(MIT); see THIRD_PARTY_NOTICES.md. Python standard library only; Linux only
(uses termios/tty for the RFCOMM device and SIOCGIF* ioctls for addresses).
"""
from __future__ import annotations

import argparse
import fcntl
import logging
import os
import select
import socket
import struct
import threading
import time
import tty

log = logging.getLogger("star-lan-bridge")

# Fixed by the protocol: StarIO clients don't let you change these.
DISCOVERY_PORT = 22222
PRINT_PORT = 9100
STATUS_PORT = 9101

# ---------------------------------------------------------------------------
# Star Discovery reply template
# ---------------------------------------------------------------------------
# 302 bytes captured from a real TSP100IIILAN (firmware V2.2). Every byte is
# sent verbatim except four fields patched per reply (see build_discovery_reply):
#   0x4E  6 bytes  MAC address
#   0x58  4 bytes  IPv4 address
#   0x6C  4 bytes  subnet mask
#   0x70  4 bytes  default gateway
# The identity strings stay as captured: short model "TSP100LAN" at 0x24,
# full model "TSP143IIILAN (STR_T-001)" at 0xCC, firmware "V2.2", manufacturer
# "Star", command set "STAR", device class "PRINTER". Full field map in
# docs/PROTOCOL.md.
DISCOVERY_TEMPLATE = bytes.fromhex(
    "5354525f424341535400000000000000"  # 0x000 "STR_BCAST" + NUL padding (16-byte cell)
    "5253312e302e3100012e00220074008a"  # 0x010 "RS1.0.1\0", total length 0x012E (=302), offset table
    "012c00525453503130304c414e000000"  # 0x020 offset table, "RTSP100LAN" (short model from 0x24)
    "0000000056322e320000000056322e32"  # 0x030 firmware "V2.2" (0x36), boot "V2.2" (0x3E)
    "00000000200000000000000031000011"  # 0x040 flags; MAC starts at 0x4E
    "6213deef00000000c0a8008444484350"  # 0x050 MAC (cont.), IP @0x58, "DHCP" @0x5C
    "000000000000000000000000ffffff00"  # 0x060 padding, netmask @0x6C
    "c0a80001001631000000000000000000"  # 0x070 gateway @0x70, flags
    "0000000030003100310000a253746172"  # 0x080 state/counter @0x88, "Star" @0x8C
    "00000000000000000000000000000000"  # 0x090
    "00000000000000000000000053544152"  # 0x0A0 "STAR" command set @0xAC
    "00000000000000000000000000000000"  # 0x0B0
    "00000000000000000000000054535031"  # 0x0C0 full model name starts @0xCC
    "34334949494c414e20285354525f542d"  # 0x0D0 "43IIILAN (STR_T-"
    "30303129000000000000000000000000"  # 0x0E0 "001)"
    "00000000000000000000000000000000"  # 0x0F0
    "0000000000000000000000005052494e"  # 0x100 device class "PRINTER" starts @0x10C
    "54455200000000000000000000000000"  # 0x110
    "0000000000000000000000000002"      # 0x120 trailer 00 02
)
assert len(DISCOVERY_TEMPLATE) == 302

# ---------------------------------------------------------------------------
# Star commands the bridge itself uses or watches for
# ---------------------------------------------------------------------------
# An ASB frame meaning "online, no errors, ETB counter 0", in LAN format. Used
# until the printer has reported its own status.
IDLE_ASB_LAN = bytes.fromhex("2386000000000000000000")

ASB_ENABLE = b"\x1b\x1e\x61\x01"   # ESC RS a 1: printer sends ASB frames whenever status changes
ASB_DISABLE = b"\x1b\x1e\x61\x00"  # ESC RS a 0: stripped. Apps send it, then poll 9101 for the
                                   # ETB counter, which only updates while ASB stays on.
STATUS_REQUEST = b"\x1b\x06\x01"   # ESC ACK SOH: one-shot status request, answered with one ASB frame


class AsbDisableFilter:
    """Remove ESC RS a 0 from a byte stream, even when split across TCP reads.

    TCP gives no guarantee that the 4 command bytes arrive in one recv(). If a
    chunk ends with the first 1-3 bytes of the command, those bytes are held
    back until the next chunk shows whether the command completes. If no more
    data arrives, the caller flushes the held bytes (they weren't the command).

    Only the exact 4-byte sequence is removed. It is short and specific, but it
    could in theory appear inside raster image data; the trade-off is accepted
    because without it StarIO clients time out and reprint (docs/PROTOCOL.md).
    """

    def __init__(self):
        self.hold = b""      # trailing partial match carried to the next feed()
        self.stripped = 0    # how many ESC RS a 0 commands were removed (for logging)

    def feed(self, data: bytes) -> bytes:
        """Return `data` (plus any held bytes) with ESC RS a 0 removed."""
        buf = self.hold + data
        n = buf.count(ASB_DISABLE)
        if n:
            self.stripped += n
            buf = buf.replace(ASB_DISABLE, b"")
        # Hold back a trailing prefix of the command so it can complete next time.
        for k in (3, 2, 1):
            if buf.endswith(ASB_DISABLE[:k]):
                self.hold = buf[-k:]
                return buf[:-k]
        self.hold = b""
        return buf

    def flush(self) -> bytes:
        """Release held bytes: the stream ended or went idle, so they weren't the command."""
        out, self.hold = self.hold, b""
        return out


# Linux ioctl numbers for reading an interface's IPv4 address and netmask.
SIOCGIFADDR = 0x8915
SIOCGIFNETMASK = 0x891B


# ---------------------------------------------------------------------------
# Network helpers
# ---------------------------------------------------------------------------
def iface_info(ifname: str):
    """Return (ip, mask, mac, gateway) for an interface as raw bytes, or None.

    Reads live values every call, so a DHCP renewal or a switch between Wi-Fi
    and Ethernet is picked up without a restart. Uses ioctls on an AF_INET
    socket instead of netlink so it works under strict systemd sandboxing.
    The gateway comes from /proc/net/route; if the interface has no default
    route, it falls back to x.y.z.1 on the same /24.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        req = struct.pack("256s", ifname.encode()[:15])
        ip = fcntl.ioctl(s.fileno(), SIOCGIFADDR, req)[20:24]      # sockaddr_in.sin_addr
        mask = fcntl.ioctl(s.fileno(), SIOCGIFNETMASK, req)[20:24]
    except OSError:
        return None  # interface missing, down, or without IPv4
    finally:
        s.close()
    try:
        with open(f"/sys/class/net/{ifname}/address") as f:
            mac = bytes.fromhex(f.read().strip().replace(":", ""))
    except OSError:
        mac = b"\x00" * 6
    gw = ip[:3] + b"\x01"
    try:
        with open("/proc/net/route") as f:
            next(f)  # header line
            for line in f:
                p = line.split()
                # Destination 00000000 = default route; gateway is little-endian hex.
                if p[0] == ifname and p[1] == "00000000":
                    gw = int(p[2], 16).to_bytes(4, "little")
                    break
    except OSError:
        pass
    return ip, mask, mac, gw


def pick_iface(client_ip: str, ifaces: list[str]):
    """Choose the interface on the querying client's subnet.

    With both Wi-Fi and Ethernet up, the reply must advertise the address the
    client can actually reach. Falls back to the first interface that is up.
    """
    c = socket.inet_aton(client_ip)
    first = None
    for name in ifaces:
        info = iface_info(name)
        if not info:
            continue
        first = first or info
        ip, mask = info[0], info[1]
        if all((a & m) == (b & m) for a, b, m in zip(ip, c, mask)):
            return info
    return first


def build_discovery_reply(info) -> bytes:
    """Patch MAC, IP, mask and gateway into the 302-byte discovery template."""
    ip, mask, mac, gw = info
    r = bytearray(DISCOVERY_TEMPLATE)
    r[0x4E:0x54] = mac
    r[0x58:0x5C] = ip
    r[0x6C:0x70] = mask
    r[0x70:0x74] = gw
    return bytes(r)


def is_discovery_query(data: bytes) -> bool:
    """Accept any UDP payload containing the STR_BCAST magic.

    Real printers answer only the exact 28-byte structured query
    ("STR_BCAST" + 7 NUL, "RQ1.0.0\\0", length 0x001C, 2-byte request id).
    Being lenient costs nothing and also serves older or diagnostic clients.
    """
    return b"STR_BCAST" in data


def discovery_thread(ifaces: list[str]):
    """UDP 22222 responder. Runs forever in a daemon thread.

    Queries arrive as subnet broadcasts (searching) or unicasts to our IP
    (checking a printer that is already configured: Next Order POS uses this
    as its "connected" test). The reply is unicast back to the sender's
    address and port, from port 22222, as a real printer does.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.bind(("0.0.0.0", DISCOVERY_PORT))  # 0.0.0.0 so broadcasts are received too
    log.info("discovery: listening on UDP %d", DISCOVERY_PORT)
    while True:
        try:
            data, addr = sock.recvfrom(2048)
        except OSError as e:
            log.warning("discovery recv error: %s", e)
            time.sleep(1)
            continue
        if not is_discovery_query(data):
            # Log unknown packets: useful when a new client speaks a variant.
            log.info("discovery: ignored %d-byte UDP packet from %s:%d: %s",
                     len(data), addr[0], addr[1], data[:32].hex(" "))
            continue
        info = pick_iface(addr[0], ifaces)
        if not info:
            log.warning("discovery: no usable interface to answer %s", addr[0])
            continue
        sock.sendto(build_discovery_reply(info), addr)
        log.info("discovery: answered %s:%d as TSP143IIILAN @ %s",
                 addr[0], addr[1], socket.inet_ntoa(info[0]))


# ---------------------------------------------------------------------------
# ASB (Automatic Status Back) helpers
# ---------------------------------------------------------------------------
def asb_size(header: int) -> int:
    """Decode an ASB frame's total length from its first byte.

    Bit 0 is always 1 (marks a status frame), bit 4 is always 0, and the
    length is split across bits 1-3 (low 3 bits) and bits 5-6 (high 2 bits):
    0x23 -> 9 bytes (USB and Bluetooth TSP143III), 0x25 -> 10, 0x27 -> 11.
    """
    return ((header & 0b1110) >> 1) | ((header & 0b1100000) >> 2)


def to_lan_asb(frame: bytes) -> bytes:
    """Convert a USB/Bluetooth ASB frame (9 bytes, byte 1 = 0x06) to LAN format.

    A TSP100IIILAN sends 11-byte frames with bit 7 of byte 1 set (0x86). StarIO
    clients talking to a "LAN" printer expect that shape, so set the bit and pad
    with zeros to 11 bytes. The status bits and the ETB counter (byte 7) are
    passed through untouched.
    """
    f = bytearray(frame)
    f[1] |= 0x80
    f.extend(b"\x00" * (11 - len(f)))
    return bytes(f[:11])


# ---------------------------------------------------------------------------
# Printer link (Bluetooth SPP via an RFCOMM TTY)
# ---------------------------------------------------------------------------
class Printer:
    """Owns the RFCOMM TTY, reconnects on failure and caches the latest status.

    Opening a bound /dev/rfcommN makes BlueZ connect to the printer, which
    takes a moment and blocks; that's acceptable because nothing can print
    until it finishes anyway. The link is then kept open between jobs, which
    removes a several-second reconnect from the start of every receipt.
    """

    def __init__(self, path: str):
        self.path = path
        self.fd: int | None = None
        self.rxbuf = b""                  # bytes from the printer not yet parsed into frames
        self.last_asb = IDLE_ASB_LAN      # most recent status, already in LAN format
        self.last_asb_time = 0.0          # when last_asb arrived (0 = never)
        self.next_retry = 0.0             # earliest time to try reopening after a failure

    def ensure_open(self) -> bool:
        """Open the TTY if needed. Retries are rate-limited so a powered-off
        printer doesn't stall the event loop on every iteration."""
        if self.fd is not None:
            return True
        if time.time() < self.next_retry:
            return False
        try:
            fd = os.open(self.path, os.O_RDWR | os.O_NOCTTY)
            tty.setraw(fd)           # no line discipline: Star data is binary
            os.write(fd, ASB_ENABLE)  # we always want unsolicited status frames
            self.fd = fd
            self.rxbuf = b""
            log.info("printer: connected via %s, ASB enabled", self.path)
            return True
        except OSError as e:
            log.warning("printer: open %s failed: %s (retry in 10s)", self.path, e)
            self.next_retry = time.time() + 10
            return False

    def close(self, why: str):
        """Drop the link after an error; the next write or loop tick reopens it."""
        if self.fd is not None:
            log.warning("printer: link closed (%s)", why)
            try:
                os.close(self.fd)
            except OSError:
                pass
        self.fd = None
        self.next_retry = time.time() + 2

    def write(self, data: bytes) -> bool:
        """Write everything (blocking). Returns False if the printer is unreachable."""
        if not self.ensure_open():
            return False
        try:
            view = memoryview(data)
            while view:
                n = os.write(self.fd, view)
                view = view[n:]
            return True
        except OSError as e:
            self.close(f"write: {e}")
            return False

    def read_frames(self) -> list[bytes]:
        """Read what's available and return complete ASB frames in LAN format.

        Frames are self-delimiting (asb_size). A byte without the frame marker
        bit can't start a frame and is skipped, which resynchronises the stream
        after line noise. Every complete frame updates the status cache.
        """
        try:
            chunk = os.read(self.fd, 4096)
        except OSError as e:
            self.close(f"read: {e}")
            return []
        if not chunk:
            self.close("EOF")
            return []
        self.rxbuf += chunk
        frames = []
        while self.rxbuf:
            size = asb_size(self.rxbuf[0])
            if not (self.rxbuf[0] & 1) or size < 2:
                log.debug("printer: dropping non-ASB byte %02x", self.rxbuf[0])
                self.rxbuf = self.rxbuf[1:]
                continue
            if len(self.rxbuf) < size:
                break  # partial frame: wait for the rest
            frame, self.rxbuf = self.rxbuf[:size], self.rxbuf[size:]
            lan = to_lan_asb(frame)
            self.last_asb, self.last_asb_time = lan, time.time()
            frames.append(lan)
        return frames

    def poll_status(self, timeout: float = 0.6):
        """Ask for a fresh status frame with ESC ACK SOH.

        Only called when no print client owns the link: injecting a command in
        the middle of a client's raster data would corrupt the job.
        """
        if not self.write(STATUS_REQUEST):
            return
        end = time.time() + timeout
        while self.fd is not None and time.time() < end:
            r, _, _ = select.select([self.fd], [], [], max(0.0, end - time.time()))
            if r and self.read_frames():
                return


# ---------------------------------------------------------------------------
# TCP side: 9100 print channel and 9101 status channel
# ---------------------------------------------------------------------------
def listen(port: int) -> socket.socket:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("0.0.0.0", port))
    s.listen(8)  # waiting print clients (e.g. CUPS behind a POS job) queue here
    return s


def serve(printer: Printer):
    """Main event loop. Never returns."""
    print_srv = listen(PRINT_PORT)
    status_srv = listen(STATUS_PORT)
    log.info("bridge: listening on TCP %d (print) and %d (status)", PRINT_PORT, STATUS_PORT)
    client: socket.socket | None = None   # the one active 9100 client, if any
    client_name = ""
    client_bytes = 0
    filt = AsbDisableFilter()

    printer.ensure_open()
    while True:
        rlist = [status_srv]
        # One print client at a time: while one is active, stop accepting new ones.
        rlist.append(client if client else print_srv)
        if printer.fd is not None:
            rlist.append(printer.fd)
        # Short timeout only while holding a partial ESC RS a 0 match, so it is
        # flushed promptly if the client goes quiet; otherwise wake every 5 s to
        # retry a lost printer link.
        r, _, _ = select.select(rlist, [], [], 0.1 if filt.hold else 5.0)
        if not r:
            if filt.hold:  # idle with a held partial match: it wasn't ESC RS a 0
                printer.write(filt.flush())
            printer.ensure_open()
            continue

        # Printer -> client: relay every status frame to the active 9100 client.
        if printer.fd is not None and printer.fd in r:
            for frame in printer.read_frames():
                if client:
                    try:
                        client.sendall(frame)
                    except OSError:
                        pass  # client went away; noticed on its next recv

        # New 9100 client: greet it with the current status, as a LAN printer does.
        if print_srv in r and client is None:
            client, addr = print_srv.accept()
            client_name, client_bytes = f"{addr[0]}:{addr[1]}", 0
            filt = AsbDisableFilter()
            log.info("9100: connection from %s", client_name)
            if printer.ensure_open():
                try:
                    client.sendall(printer.last_asb)
                except OSError:
                    pass

        # Client -> printer: forward print data (minus ESC RS a 0).
        if client and client in r:
            try:
                data = client.recv(65536)
            except OSError:
                data = b""
            if data:
                client_bytes += len(data)
                if not printer.write(filt.feed(data)):
                    log.warning("9100: printer unavailable, dropping %s", client_name)
                    client.close()
                    client = None
            else:
                # Client closed its side: the job is complete.
                printer.write(filt.flush())
                log.info("9100: %s finished, %d bytes received, %d ASB-disable cmd(s) stripped, "
                         "status now %s", client_name, client_bytes, filt.stripped,
                         printer.last_asb.hex(" "))
                client.close()
                client = None
                printer.write(ASB_ENABLE)  # belt and braces: make sure ASB is still on

        # 9101 poll: read the query (content isn't checked), answer with the
        # latest real status twice, close. StarIO polls this every ~200 ms
        # during a job, so it must stay fast.
        if status_srv in r:
            conn, addr = status_srv.accept()
            conn.settimeout(0.3)
            try:
                query = conn.recv(64)
            except OSError:
                query = b""
            # Between jobs the cache can be stale; refresh it if nothing owns the link.
            if client is None and time.time() - printer.last_asb_time > 1.0:
                printer.poll_status()
            try:
                conn.sendall(printer.last_asb * 2)
            except OSError:
                pass
            conn.close()
            log.debug("9101: %s query %d bytes -> %s", addr[0], len(query), printer.last_asb.hex(" "))


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("-d", "--device", default="/dev/rfcomm0",
                    help="RFCOMM TTY bound to the printer (default: /dev/rfcomm0)")
    ap.add_argument("-i", "--iface", action="append",
                    help="interface to advertise in discovery replies; repeat for several "
                         "(default: wlan0 then eth0)")
    ap.add_argument("-v", "--verbose", action="store_true", help="debug logging (logs every 9101 poll)")
    a = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(levelname)s %(message)s")
    ifaces = a.iface or ["wlan0", "eth0"]
    threading.Thread(target=discovery_thread, args=(ifaces,), daemon=True).start()
    serve(Printer(a.device))


if __name__ == "__main__":
    main()
