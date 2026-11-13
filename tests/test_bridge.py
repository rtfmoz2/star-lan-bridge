# SPDX-License-Identifier: MIT
"""Unit tests for the protocol pieces of star_lan_bridge (no hardware or network needed).

Run from the repository root:  python3 -m unittest discover tests
"""
import importlib.util
import os
import random
import socket
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load(name, rel):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, rel))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


bridge = load("star_lan_bridge", "star_lan_bridge.py")
probe = load("star_probe", os.path.join("tools", "star_probe.py"))

D = bridge.ASB_DISABLE


class AsbDisableFilterTest(unittest.TestCase):
    def run_chunks(self, stream, sizes):
        f = bridge.AsbDisableFilter()
        out, i = b"", 0
        for n in sizes:
            out += f.feed(stream[i:i + n])
            i += n
        out += f.feed(stream[i:]) + f.flush()
        return out, f.stripped

    def test_strips_whole_command(self):
        out, n = self.run_chunks(b"AB" + D + b"CD", [100])
        self.assertEqual(out, b"ABCD")
        self.assertEqual(n, 1)

    def test_strips_command_split_at_every_position(self):
        stream = b"\x1b\x2a\x72\x42" + D + b"\x17"
        for cut in range(len(stream) + 1):
            out, n = self.run_chunks(stream, [cut])
            self.assertEqual(out, b"\x1b\x2a\x72\x42\x17", f"cut at {cut}")
            self.assertEqual(n, 1)

    def test_keeps_asb_enable_and_lookalikes(self):
        keep = bridge.ASB_ENABLE + b"\x1b\x1e\x61" + b"\x1b\x1e\x45\x00" + b"\x1b"
        out, n = self.run_chunks(keep, [1, 2, 3])
        self.assertEqual(out, keep)
        self.assertEqual(n, 0)

    def test_held_prefix_is_released_by_flush(self):
        f = bridge.AsbDisableFilter()
        self.assertEqual(f.feed(b"xy\x1b\x1e"), b"xy")
        self.assertEqual(f.hold, b"\x1b\x1e")
        self.assertEqual(f.flush(), b"\x1b\x1e")
        self.assertEqual(f.hold, b"")

    def test_random_streams_match_single_pass(self):
        rng = random.Random(1234)
        alphabet = [0x1B, 0x1E, 0x61, 0x00, 0x01, 0x17, 0x41]
        for _ in range(3000):
            parts = [bytes(rng.choice(alphabet) for _ in range(rng.randint(0, 30))) for _ in range(5)]
            stream = D.join(parts)
            sizes = [rng.randint(1, 7) for _ in range(len(stream))]
            out, _ = self.run_chunks(stream, sizes)
            # Chunked filtering must give the same result as filtering the whole stream at once.
            whole = bridge.AsbDisableFilter()
            expected = whole.feed(stream) + whole.flush()
            self.assertEqual(out, expected, stream.hex())
            self.assertNotIn(D, expected.replace(D, b"") if D in expected else expected)


class AsbFrameTest(unittest.TestCase):
    def test_sizes(self):
        self.assertEqual(bridge.asb_size(0x23), 9)
        self.assertEqual(bridge.asb_size(0x25), 10)
        self.assertEqual(bridge.asb_size(0x27), 11)

    def test_bluetooth_frame_to_lan(self):
        bt = bytes.fromhex("230602000000000200")  # observed after one ETB
        lan = bridge.to_lan_asb(bt)
        self.assertEqual(lan.hex(" "), "23 86 02 00 00 00 00 02 00 00 00")

    def test_lan_frame_unchanged(self):
        lan = bytes.fromhex("2386000000000000000000")
        self.assertEqual(bridge.to_lan_asb(lan), lan)

    def test_printer_parses_split_and_concatenated_frames(self):
        p = bridge.Printer("/dev/null")
        r, w = os.pipe()
        p.fd = r
        os.write(w, bytes.fromhex("2306000000000000002306"))  # one frame + start of next
        frames = p.read_frames()
        self.assertEqual(len(frames), 1)
        os.write(w, bytes.fromhex("02000000000200"))          # rest of second frame
        frames = p.read_frames()
        self.assertEqual([f.hex() for f in frames], ["2386020000000002000000"])
        self.assertEqual(p.last_asb, frames[0])
        os.close(r)
        os.close(w)


class DiscoveryTest(unittest.TestCase):
    INFO = (socket.inet_aton("192.168.1.134"), socket.inet_aton("255.255.255.0"),
            bytes.fromhex("dca632aabbcc"), socket.inet_aton("192.168.1.1"))

    def test_template_shape(self):
        t = bridge.DISCOVERY_TEMPLATE
        self.assertEqual(len(t), 302)
        self.assertEqual(t[:9], b"STR_BCAST")
        self.assertEqual(t[0x10:0x18], b"RS1.0.1\0")
        self.assertEqual(int.from_bytes(t[0x18:0x1A], "big"), 302)

    def test_reply_patches_only_network_fields(self):
        reply = bridge.build_discovery_reply(self.INFO)
        info = probe.decode_discovery(reply)
        self.assertEqual(info["ip"], "192.168.1.134")
        self.assertEqual(info["netmask"], "255.255.255.0")
        self.assertEqual(info["gateway"], "192.168.1.1")
        self.assertEqual(info["mac"], "dc:a6:32:aa:bb:cc")
        self.assertEqual(info["model"], "TSP143IIILAN (STR_T-001)")
        self.assertEqual(info["manufacturer"], "Star")
        self.assertEqual(info["device_class"], "PRINTER")
        patched = set(range(0x4E, 0x54)) | set(range(0x58, 0x5C)) | set(range(0x6C, 0x74))
        for i, (a, b) in enumerate(zip(reply, bridge.DISCOVERY_TEMPLATE)):
            if i not in patched:
                self.assertEqual(a, b, f"byte 0x{i:03x} changed")

    def test_query_detection(self):
        self.assertTrue(bridge.is_discovery_query(probe.QUERY))
        self.assertEqual(len(probe.QUERY), 28)
        self.assertTrue(bridge.is_discovery_query(b"xxSTR_BCASTyy"))
        self.assertFalse(bridge.is_discovery_query(b"EPSONQ\0\0\0\0\0\0\0\0"))


class ConfigTest(unittest.TestCase):
    def write(self, text):
        import tempfile
        f = tempfile.NamedTemporaryFile("w", suffix=".conf", delete=False)
        f.write(text)
        f.close()
        self.addCleanup(os.unlink, f.name)
        return f.name

    def lease_dir(self, leases):
        import tempfile
        d = tempfile.mkdtemp()
        for name, ip in leases.items():
            with open(os.path.join(d, f"lease-{name}"), "w") as f:
                f.write(f"ip={ip}\nmask=24\nlease=43200\n")
        self.addCleanup(lambda: [os.unlink(os.path.join(d, x)) for x in os.listdir(d)] and os.rmdir(d))
        return d

    def test_example_config_parses(self):
        plist = bridge.read_printer_config(os.path.join(ROOT, "examples", "star-lan-bridge.conf"))
        self.assertEqual([p["name"] for p in plist], ["TSP143", "TSP143-2"])
        self.assertEqual([p["address"] for p in plist], ["auto", "dhcp"])
        self.assertEqual(plist[1]["device"], "/dev/rfcomm1")
        self.assertEqual(plist[1]["local"], "127.0.0.2")

    def test_dhcp_printer_uses_its_lease(self):
        path = self.write("[printer:a]\ndevice=/dev/rfcomm0\naddress=10.0.0.5\nlocal=127.0.0.1\n"
                          "[printer:b]\ndevice=/dev/rfcomm1\naddress=dhcp\nlocal=127.0.0.2\nmac=00:11:62:2e:75:05\n")
        stations = bridge.load_config(path, lease_dir=self.lease_dir({"b": "10.0.0.37"}))
        a, b = stations
        self.assertEqual(a.listen_addrs(), ["10.0.0.5", "127.0.0.1"])
        self.assertEqual(b.listen_addrs(), ["10.0.0.37", "127.0.0.2"])
        self.assertEqual(b.mac, bytes.fromhex("0011622e7505"))

    def test_printer_without_lease_is_skipped(self):
        path = self.write("[printer:a]\ndevice=/dev/rfcomm0\naddress=10.0.0.5\n"
                          "[printer:b]\ndevice=/dev/rfcomm1\naddress=dhcp\n")
        stations = bridge.load_config(path, lease_dir=self.lease_dir({}))
        self.assertEqual([s.name for s in stations], ["a"])

    def test_bad_lease_file_is_ignored(self):
        d = self.lease_dir({"b": "not-an-ip"})
        self.assertIsNone(bridge.read_lease("b", d))
        self.assertIsNone(bridge.read_lease("missing", d))

    def test_resolve_fixed_and_errors(self):
        self.assertEqual(bridge.resolve_address("a", "10.1.2.3"), "10.1.2.3")
        with self.assertRaises(OSError):
            bridge.resolve_address("a", "not-an-ip")

    def test_bad_printer_name_rejected(self):
        with self.assertRaises(ValueError):
            bridge.read_printer_config(self.write("[printer:../etc]\ndevice=/dev/rfcomm0\naddress=dhcp\n"))

    def test_duplicate_address_rejected(self):
        path = self.write("[printer:a]\ndevice=/dev/rfcomm0\naddress=10.0.0.5\n"
                          "[printer:b]\ndevice=/dev/rfcomm1\naddress=10.0.0.5\n")
        with self.assertRaises(ValueError):
            bridge.load_config(path)

    def test_missing_fields_rejected(self):
        with self.assertRaises(ValueError):
            bridge.load_config(self.write("[printer:a]\ndevice=/dev/rfcomm0\n"))
        with self.assertRaises(ValueError):
            bridge.load_config(self.write("[other]\nx=1\n"))
        with self.assertRaises(OSError):
            bridge.load_config(self.write("[printer:a]\ndevice=/dev/rfcomm0\naddress=not-an-ip\n"))
        with self.assertRaises(ValueError):
            bridge.read_printer_config(self.write("[printer:a]\naddress=dhcp\n"))  # no device

    def test_single_printer_mode_listens_everywhere(self):
        st = bridge.Station("printer", "/dev/rfcomm0")
        self.assertEqual(st.listen_addrs(), ["0.0.0.0"])

    def test_parse_mac(self):
        self.assertEqual(bridge.parse_mac("00-11-62-2E-75-05"), bytes.fromhex("0011622e7505"))
        with self.assertRaises(ValueError):
            bridge.parse_mac("00:11:62")


class ProbeTest(unittest.TestCase):
    def test_etb_counter_decode(self):
        self.assertIn("etb_counter=1", probe.asb_fields(bytes.fromhex("2386020000000002000000")))
        self.assertIn("etb_counter=0", probe.asb_fields(bytes.fromhex("2386000000000000000000")))
        self.assertIn("iface=USB/BT", probe.asb_fields(bytes.fromhex("230600000000000000")))


class PrinterLinkTest(unittest.TestCase):
    """Reconnect bookkeeping, without a real RFCOMM device."""

    def test_unreachable_sets_offline_and_backs_off(self):
        p = bridge.Printer("/dev/rfcomm-does-not-exist")
        p.last_asb = bytes.fromhex("2386000000000002000000")
        t0 = bridge.time.time()
        self.assertFalse(p.ensure_open())          # open fails: no hang, no exception
        self.assertEqual(p.last_asb[2] & 0x08, 0x08)  # offline bit set
        self.assertEqual(p.last_asb[7], 0x02)         # ETB counter kept
        self.assertGreaterEqual(p.next_retry - t0, p.MIN_BACKOFF - 0.5)
        first = p.backoff
        p.next_retry = 0
        p.ensure_open()
        self.assertGreaterEqual(p.backoff, first)
        self.assertLessEqual(p.backoff, p.MAX_BACKOFF)

    def test_write_when_down_returns_false_fast(self):
        p = bridge.Printer("/dev/rfcomm-does-not-exist")
        t0 = bridge.time.time()
        self.assertFalse(p.write(b"\x1b\x06\x01"))
        self.assertFalse(p.write(b"x", wait=0.6))
        self.assertLess(bridge.time.time() - t0, 3 * float(os.environ.get("STAR_TEST_TIME_SCALE", "1")))


if __name__ == "__main__":
    unittest.main()
