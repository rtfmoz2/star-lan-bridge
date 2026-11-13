# SPDX-License-Identifier: MIT
"""Fuzz and abuse tests: run the real bridge loop against a fake printer.

The "printer" is a pseudo-terminal standing in for /dev/rfcommN, so these run
on Linux or macOS without Bluetooth. They check that hostile or random input on
TCP 9100, TCP 9101 and UDP 22222, or garbage from the printer, never crashes
the bridge, never stops it serving the next legitimate client, and never
changes the print data beyond removing ESC RS a 0.

Run from the repository root:  python3 -m unittest tests.test_fuzz
"""
import importlib.util
import os
import pty
import random
import select
import socket
import threading
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location("slb_fuzz", os.path.join(ROOT, "star_lan_bridge.py"))
bridge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bridge)

D = bridge.ASB_DISABLE
HOST = "127.0.0.1"

# Shared CI machines can be much slower than a laptop. CI sets
# STAR_TEST_TIME_SCALE (e.g. 3) to stretch every wait and time limit here;
# what's being checked stays the same.
SCALE = float(os.environ.get("STAR_TEST_TIME_SCALE", "1"))


def T(seconds: float) -> float:
    return seconds * SCALE


def free_port() -> int:
    s = socket.socket()
    s.bind((HOST, 0))
    port = s.getsockname()[1]
    s.close()
    return port


class FakePrinter:
    """Master side of a pty: records what the bridge sends and can reply with ASB frames."""

    def __init__(self):
        self.master, slave = pty.openpty()
        self.path = os.ttyname(slave)
        self.slave = slave  # keep open so the pty stays alive
        self.received = bytearray()
        self.lock = threading.Lock()
        threading.Thread(target=self._drain, daemon=True).start()

    def _drain(self):
        while True:
            r, _, _ = select.select([self.master], [], [], 0.2)
            if r:
                try:
                    data = os.read(self.master, 65536)
                except OSError:
                    return
                with self.lock:
                    self.received += data

    def take(self) -> bytes:
        with self.lock:
            out = bytes(self.received)
            self.received.clear()
            return out

    def send(self, data: bytes):
        os.write(self.master, data)


class BridgeHarness(unittest.TestCase):
    """Starts one Station on high loopback ports with a fake printer."""

    @classmethod
    def setUpClass(cls):
        bridge.Printer.link_state = lambda self: "connected"   # a pty is always "connected"
        bridge.log.disabled = True
        cls.print_port, cls.status_port = free_port(), free_port()
        bridge.PRINT_PORT, bridge.STATUS_PORT = cls.print_port, cls.status_port
        bridge.JOB_IDLE_TIMEOUT = 1.5
        bridge.JOB_MAX_BYTES = 256 * 1024
        bridge.STATUS_LIMIT = bridge.RateLimiter(rate=1000, burst=1000, total_rate=1000)
        cls.printer = FakePrinter()
        cls.station = bridge.Station("fuzz", cls.printer.path, address=HOST)
        cls.thread = threading.Thread(target=bridge.serve, args=(cls.station,), daemon=True)
        cls.thread.start()
        time.sleep(T(0.5))
        cls.printer.take()  # discard ASB enable / status request sent on connect

    def setUp(self):
        self.assertTrue(self.thread.is_alive(), "bridge loop died")

    def wait_received(self, n: int, timeout: float = 5.0) -> bytes:
        timeout = T(timeout)
        out, end = bytearray(), time.time() + timeout
        while len(out) < n and time.time() < end:
            out += self.printer.take()
            time.sleep(0.02)
        time.sleep(0.1)
        return bytes(out + self.printer.take())

    def job(self, data: bytes, chunk: int = 1500) -> socket.socket:
        c = socket.create_connection((HOST, self.print_port), timeout=T(5))
        for i in range(0, len(data), chunk):
            c.sendall(data[i:i + chunk])
        return c

    def status(self, payload: bytes = b"\x32" + b"\0" * 50) -> bytes:
        c = socket.create_connection((HOST, self.status_port), timeout=T(5))
        c.sendall(payload)
        out = b""
        c.settimeout(T(2))
        try:
            while True:
                x = c.recv(64)
                if not x:
                    break
                out += x
        except (socket.timeout, ConnectionResetError):
            pass  # a reset is fine: the bridge closed on an oversized garbage query
        c.close()
        return out


class PrintChannelFuzz(BridgeHarness):
    def test_random_jobs_pass_through_exactly(self):
        rng = random.Random(42)
        alphabet = [0x1B, 0x1E, 0x61, 0x00, 0x01, 0x17, 0x06, 0x2A, 0x72, 0x42]
        for i in range(40):
            parts = []
            for _ in range(rng.randint(1, 6)):
                if rng.random() < 0.5:
                    parts.append(bytes(rng.choice(alphabet) for _ in range(rng.randint(0, 300))))
                else:
                    parts.append(os.urandom(rng.randint(0, 4000)))
            data = D.join(parts)
            expected = data.replace(D, b"")  # what a correct bridge forwards
            c = self.job(data, chunk=rng.randint(1, 3000))
            c.shutdown(socket.SHUT_WR)
            got = self.wait_received(len(expected))
            c.close()
            # The bridge appends ESC RS a 1 after each job; strip that trailer.
            if got.endswith(bridge.ASB_ENABLE):
                got = got[: -len(bridge.ASB_ENABLE)]
            self.assertEqual(got, expected, f"job {i}: {len(got)} vs {len(expected)} bytes")
            time.sleep(0.05)

    def test_idle_client_is_dropped_and_next_client_served(self):
        idle = socket.create_connection((HOST, self.print_port), timeout=T(5))
        idle.recv(11)  # greeting frame
        time.sleep(T(0.3))
        nxt = socket.create_connection((HOST, self.print_port), timeout=T(5))  # waits in backlog
        nxt.sendall(b"HELLO")
        nxt.shutdown(socket.SHUT_WR)
        got = self.wait_received(5, timeout=6)
        self.assertIn(b"HELLO", got)
        idle.settimeout(T(1))
        try:
            leftover = idle.recv(64)
        except ConnectionResetError:
            leftover = b""
        self.assertEqual(leftover, b"", "idle client should have been closed")
        idle.close()
        nxt.close()

    def test_oversized_job_is_cut_off(self):
        c = socket.create_connection((HOST, self.print_port), timeout=T(5))
        c.recv(11)
        sent = 0
        try:
            while sent < 2 * bridge.JOB_MAX_BYTES:
                c.sendall(b"\x00" * 16384)
                sent += 16384
        except OSError:
            pass  # the bridge closed us: expected
        got = self.wait_received(bridge.JOB_MAX_BYTES, timeout=5)
        self.assertLessEqual(len(got), bridge.JOB_MAX_BYTES + 65536)
        c.close()
        # still serves a normal job afterwards
        c = self.job(b"after")
        c.shutdown(socket.SHUT_WR)
        self.assertIn(b"after", self.wait_received(5))
        c.close()

    def test_half_open_and_reset_connections(self):
        for _ in range(30):
            c = socket.create_connection((HOST, self.print_port), timeout=T(5))
            c.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, b"\x01\x00\x00\x00\x00\x00\x00\x00")
            c.close()  # RST
        c = self.job(b"still-alive")
        c.shutdown(socket.SHUT_WR)
        self.assertIn(b"still-alive", self.wait_received(11))
        c.close()


class StatusChannelFuzz(BridgeHarness):
    def test_garbage_queries_get_status_or_nothing(self):
        for payload in [b"", b"\x00", os.urandom(4096), b"\x32" * 51, b"GET / HTTP/1.0\r\n\r\n"]:
            out = self.status(payload)
            self.assertIn(len(out), (0, 22))
        self.assertEqual(len(self.status()), 22)

    def test_silent_clients_do_not_stall(self):
        hogs = [socket.create_connection((HOST, self.status_port), timeout=T(5)) for _ in range(40)]
        t0 = time.time()
        self.assertEqual(len(self.status()), 22)
        self.assertLess(time.time() - t0, T(1.5), "silent status clients stalled the bridge")
        # and print data still flows while they hang around
        c = self.job(b"through-the-crowd")
        c.shutdown(socket.SHUT_WR)
        self.assertIn(b"through-the-crowd", self.wait_received(17))
        c.close()
        for h in hogs:
            h.close()


class PrinterSideFuzz(BridgeHarness):
    def test_garbage_from_printer_then_valid_frame(self):
        self.printer.send(os.urandom(20000))
        time.sleep(T(0.3))
        self.printer.send(bytes.fromhex("230602000000000200"))  # valid frame: ETB executed, counter 1
        time.sleep(T(0.3))
        out = self.status()
        self.assertEqual(len(out), 22)
        self.assertTrue(self.thread.is_alive())


class DiscoveryFuzz(unittest.TestCase):
    def test_junk_udp_never_kills_the_responder(self):
        bridge.log.disabled = True
        port = free_port()
        bridge.DISCOVERY_PORT = port
        st = bridge.Station("d", "/dev/null", address=HOST)
        t = threading.Thread(target=bridge.discovery_thread, args=([st], ["lo0", "lo"]), daemon=True)
        t.start()
        time.sleep(T(0.3))
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        for i in range(500):
            junk = os.urandom(random.randint(0, 1400))
            if i % 7 == 0:
                junk = b"STR_BCAST" + junk
            s.sendto(junk, (HOST, port))
        s.close()
        time.sleep(T(0.5))
        self.assertTrue(t.is_alive())


class PolicyTest(unittest.TestCase):
    def test_local_subnets_only(self):
        p = bridge.AccessPolicy()
        p.local_networks = lambda: [bridge.ipaddress.ip_network("192.168.1.0/24")]
        p.stamp = 0
        self.assertTrue(p.allowed("127.0.0.1"))
        self.assertTrue(p.allowed("192.168.1.141"))
        self.assertFalse(p.allowed("192.168.2.10"))
        self.assertFalse(p.allowed("8.8.8.8"))
        self.assertFalse(p.allowed("192.168.1.255"))   # broadcast: spoofed source
        self.assertFalse(p.allowed("192.168.1.0"))
        self.assertFalse(p.allowed("224.0.0.251"))
        self.assertFalse(p.allowed("0.0.0.0"))
        self.assertFalse(p.allowed("not-an-ip"))

    def test_rate_limiter(self):
        rl = bridge.RateLimiter(rate=1, burst=3, total_rate=1000)
        self.assertEqual(sum(rl.allow("a") for _ in range(10)), 3)
        self.assertTrue(rl.allow("b"))
        overall = bridge.RateLimiter(rate=100, burst=100, total_rate=5)
        self.assertEqual(sum(overall.allow(str(i)) for i in range(50)), 5)

    def test_log_throttle(self):
        calls = []

        class L:
            def log(self, level, msg, *a):
                calls.append(msg % a)
        th = bridge.LogThrottle(every=60)
        for i in range(100):
            th(L(), 20, "k", "event %d", i)
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
