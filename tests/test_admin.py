# SPDX-License-Identifier: MIT
"""Unit tests for star_admin (no network, NetworkManager or root needed).

Run from the repository root:  python3 -m unittest discover tests
"""
import importlib.util
import os
import unittest
from configparser import RawConfigParser

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location("star_admin", os.path.join(ROOT, "star_admin.py"))
admin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(admin)


class NmcliParsingTest(unittest.TestCase):
    def test_plain_fields(self):
        self.assertEqual(admin.nm_split("*:SSID:57:WPA2:64"), ["*", "SSID", "57", "WPA2", "64"])

    def test_escaped_colon_and_backslash(self):
        self.assertEqual(admin.nm_split(r" :Cafe\:Guest:40:WPA2:6"), [" ", "Cafe:Guest", "40", "WPA2", "6"])
        self.assertEqual(admin.nm_split(r"a\\b:c"), ["a\\b", "c"])

    def test_empty_fields(self):
        self.assertEqual(admin.nm_split("uuid:lo:loopback:no:0:"), ["uuid", "lo", "loopback", "no", "0", ""])


class ValidationTest(unittest.TestCase):
    def test_ssid(self):
        self.assertEqual(admin.validate_ssid("Venue Staff"), "Venue Staff")
        for bad in ["", "x" * 33, "bad\nname", "tab\tname"]:
            with self.assertRaises(ValueError):
                admin.validate_ssid(bad)
        self.assertEqual(admin.validate_ssid("café"), "café")  # 5 bytes, fine

    def test_psk(self):
        self.assertEqual(admin.validate_psk(""), "")
        self.assertEqual(admin.validate_psk("correct horse"), "correct horse")
        for bad in ["short", "x" * 64, "new\nline", "naïve-password"]:
            with self.assertRaises(ValueError):
                admin.validate_psk(bad)


class KeyfileTest(unittest.TestCase):
    def parse(self, text):
        cp = RawConfigParser(delimiters=("=",), interpolation=None)
        cp.optionxform = str
        cp.read_string(text)
        return cp

    def test_wpa_network(self):
        kf = self.parse(admin.build_keyfile("Venue Staff", "s3cret-pass", "1234", hidden=False))
        self.assertEqual(kf["connection"]["id"], "Venue Staff")
        self.assertEqual(kf["connection"]["type"], "wifi")
        self.assertEqual(kf["connection"]["interface-name"], "wlan0")
        self.assertEqual(kf["wifi"]["ssid"], "Venue Staff")
        self.assertEqual(kf["wifi-security"]["key-mgmt"], "wpa-psk")
        self.assertEqual(kf["wifi-security"]["psk"], "s3cret-pass")
        self.assertEqual(kf["ipv4"]["method"], "auto")
        self.assertNotIn("hidden", kf["wifi"])

    def test_open_hidden_network(self):
        kf = self.parse(admin.build_keyfile("Back Office", "", "1234", hidden=True))
        self.assertNotIn("wifi-security", kf)
        self.assertEqual(kf["wifi"]["hidden"], "true")

    def test_escaping_keeps_one_value_per_line(self):
        text = admin.build_keyfile(" lead\\slash", "pa=ss;word #1", "1234", hidden=False)
        kf = self.parse(text)
        self.assertEqual(kf["wifi"]["ssid"], "\\slead\\\\slash")
        self.assertEqual(kf["wifi-security"]["psk"], "pa=ss;word #1")


class PasswordTest(unittest.TestCase):
    def test_round_trip(self):
        rec = admin.hash_password("a long admin password", iterations=1000)
        self.assertTrue(admin.check_password("a long admin password", rec))
        self.assertFalse(admin.check_password("a long admin passwore", rec))
        self.assertNotIn("a long admin password", str(rec))

    def test_unique_salts(self):
        a = admin.hash_password("same", iterations=1000)
        b = admin.hash_password("same", iterations=1000)
        self.assertNotEqual(a["salt"], b["salt"])
        self.assertNotEqual(a["hash"], b["hash"])


class SessionTest(unittest.TestCase):
    def test_tokens_and_throttle(self):
        s = admin.Sessions()
        tok = s.new()
        self.assertTrue(s.valid(tok))
        self.assertFalse(s.valid("nope"))
        self.assertFalse(s.valid(None))
        s.drop(tok)
        self.assertFalse(s.valid(tok))
        for _ in range(4):
            s.failed("10.0.0.9")
        self.assertFalse(s.throttled("10.0.0.9"))
        s.failed("10.0.0.9")
        self.assertTrue(s.throttled("10.0.0.9"))
        self.assertFalse(s.throttled("10.0.0.10"))


class StatusTextTest(unittest.TestCase):
    def frame(self, hexstr):
        return bytes.fromhex(hexstr)

    def test_ready(self):
        st = admin.describe_status(self.frame("2386000000000002000000"))  # ETB counter alone is fine
        self.assertEqual(st["state"], "ok")

    def test_paper_out(self):
        st = admin.describe_status(self.frame("2386080000080002000000"))  # seen with no paper loaded
        self.assertEqual(st["text"], "Out of paper")

    def test_unknown(self):
        self.assertEqual(admin.describe_status(None)["state"], "unknown")


class DhcpHelpersTest(unittest.TestCase):
    def test_client_id_is_stable_and_distinct(self):
        a = admin.client_id("dc:a6:32:00:00:01", "TSP143-2")
        self.assertEqual(a, admin.client_id("DC:A6:32:00:00:01", "TSP143-2"))
        self.assertNotEqual(a, admin.client_id("dc:a6:32:00:00:01", "TSP143-3"))
        self.assertNotEqual(a, admin.client_id("dc:a6:32:00:00:02", "TSP143-2"))
        self.assertTrue(a.startswith("00"))
        self.assertEqual(bytes.fromhex(a[2:]).decode(), "star-lan-bridge:dca632000001:TSP143-2")

    def test_hostname_is_dns_safe(self):
        h = admin.dhcp_hostname("TSP143_2.x")
        self.assertRegex(h, r"^[a-z0-9-]+$")
        self.assertLessEqual(len(admin.dhcp_hostname("x" * 100)), 63)


class AliasScriptTest(unittest.TestCase):
    """Run star-dhcp-alias.sh with stub `ip` and `logger` commands."""

    def run_hook(self, event, env_extra, lease_dir):
        import subprocess, tempfile
        bindir = tempfile.mkdtemp()
        calls = os.path.join(bindir, "calls")
        for tool in ("ip", "logger"):
            path = os.path.join(bindir, tool)
            with open(path, "w") as f:
                f.write(f'#!/bin/sh\necho "{tool} $*" >> "{calls}"\n')
            os.chmod(path, 0o755)
        env = {"PATH": bindir + ":/usr/bin:/bin", "LEASE_DIR": lease_dir, "PRINTER": "TSP143-2",
               "interface": "wlan0", **env_extra}
        r = subprocess.run(["sh", os.path.join(ROOT, "star-dhcp-alias.sh"), event], env=env,
                           capture_output=True, text=True)
        out = ""
        if os.path.exists(calls):
            with open(calls) as f:
                out = f.read()
        return r.returncode, out

    def test_bound_then_new_address_then_fail(self):
        import tempfile
        d = tempfile.mkdtemp()
        lease = {"ip": "192.168.50.137", "mask": "24", "lease": "43200", "router": "192.168.50.1", "serverid": "192.168.50.1"}
        rc, calls = self.run_hook("bound", lease, d)
        self.assertEqual(rc, 0)
        self.assertIn("ip addr replace 192.168.50.137/24 dev wlan0 valid_lft 43200 preferred_lft 43200 noprefixroute", calls)
        with open(os.path.join(d, "lease-TSP143-2")) as f:
            rec = f.read()
        self.assertIn("ip=192.168.50.137", rec)
        self.assertIn("interface=wlan0", rec)

        rc, calls = self.run_hook("renew", {**lease, "ip": "192.168.50.140"}, d)
        self.assertIn("ip addr del 192.168.50.137/24 dev wlan0", calls)
        self.assertIn("ip addr replace 192.168.50.140/24", calls)

        rc, calls = self.run_hook("leasefail", {}, d)
        self.assertIn("ip addr del 192.168.50.140/24 dev wlan0", calls)
        self.assertFalse(os.path.exists(os.path.join(d, "lease-TSP143-2")))

    def test_rejects_bad_input(self):
        import tempfile
        d = tempfile.mkdtemp()
        rc, calls = self.run_hook("bound", {"ip": "1.2.3.4; reboot", "mask": "24"}, d)
        self.assertNotEqual(rc, 0)
        self.assertNotIn("ip addr", calls)
        rc, _ = self.run_hook("bound", {"ip": "1.2.3.4", "PRINTER": "../x"}, d)
        self.assertNotEqual(rc, 0)


class ClashHookTest(AliasScriptTest):
    """The hook refuses a lease for an address the interface already has."""

    def run_hook_with_addrs(self, event, env_extra, lease_dir, addrs):
        import subprocess, tempfile
        bindir = tempfile.mkdtemp()
        calls = os.path.join(bindir, "calls")
        with open(os.path.join(bindir, "ip"), "w") as f:
            f.write(f'#!/bin/sh\necho "ip $*" >> "{calls}"\n'
                    f'if [ "$1 $2 $3" = "-4 -o addr" ]; then\n'
                    + "".join(f'  echo "3: wlan0    inet {a}/24 brd x scope global wlan0"\n' for a in addrs)
                    + "fi\n")
        with open(os.path.join(bindir, "logger"), "w") as f:
            f.write(f'#!/bin/sh\necho "logger $*" >> "{calls}"\n')
        for t in ("ip", "logger"):
            os.chmod(os.path.join(bindir, t), 0o755)
        env = {"PATH": bindir + ":/usr/bin:/bin", "LEASE_DIR": lease_dir, "PRINTER": "TSP143-2",
               "interface": "wlan0", **env_extra}
        r = subprocess.run(["sh", os.path.join(ROOT, "star-dhcp-alias.sh"), event], env=env,
                           capture_output=True, text=True)
        with open(calls) as f:
            return r.returncode, f.read()

    def test_lease_for_pis_own_address_is_refused(self):
        import tempfile
        d = tempfile.mkdtemp()
        lease = {"ip": "192.168.50.10", "mask": "24", "lease": "86400"}
        rc, calls = self.run_hook_with_addrs("bound", lease, d, ["192.168.50.10"])
        self.assertEqual(rc, 0)
        self.assertNotIn("ip addr replace", calls)
        self.assertFalse(os.path.exists(os.path.join(d, "lease-TSP143-2")))
        with open(os.path.join(d, "clash-TSP143-2")) as f:
            self.assertIn("ip=192.168.50.10", f.read())

    def test_similar_address_is_not_a_clash(self):
        import tempfile
        d = tempfile.mkdtemp()
        lease = {"ip": "192.168.50.1", "mask": "24", "lease": "86400"}
        rc, calls = self.run_hook_with_addrs("bound", lease, d, ["192.168.50.10"])
        self.assertIn("ip addr replace 192.168.50.1/24", calls)
        self.assertFalse(os.path.exists(os.path.join(d, "clash-TSP143-2")))


class ArpTest(unittest.TestCase):
    MAC = bytes.fromhex("dca632000001")
    OTHER = bytes.fromhex("aabbccddeeff")
    IP = bytes([192, 168, 50, 11])

    def reply(self, sha, spa, op=2, tpa=b"\0" * 4):
        import struct
        return (b"\xff" * 6 + sha + b"\x08\x06"
                + struct.pack("!HHBBH6s4s6s4s", 1, 0x0800, 6, 4, op, sha, spa, b"\0" * 6, tpa))

    def test_probe_frame(self):
        f = admin.arp_frame(self.MAC, b"\0" * 4, self.IP)
        self.assertEqual(len(f), 60)
        self.assertEqual(f[:6], b"\xff" * 6)
        self.assertEqual(f[12:14], b"\x08\x06")
        self.assertEqual(f[20:22], b"\x00\x01")       # request
        self.assertEqual(f[28:32], b"\0" * 4)          # sender IP 0.0.0.0: a probe
        self.assertEqual(f[38:42], self.IP)

    def test_conflicts(self):
        c = admin.arp_conflict
        self.assertTrue(c(self.reply(self.OTHER, self.IP), self.IP, self.MAC))           # someone has it
        self.assertTrue(c(self.reply(self.OTHER, b"\0" * 4, op=1, tpa=self.IP), self.IP, self.MAC))  # probing
        self.assertFalse(c(self.reply(self.MAC, self.IP), self.IP, self.MAC))            # our own frame
        self.assertFalse(c(self.reply(self.OTHER, bytes([192, 168, 50, 1]), op=1, tpa=self.IP),
                           self.IP, self.MAC))                                           # router asking for it
        self.assertFalse(c(b"\0" * 20, self.IP, self.MAC))


class NextCandidatesTest(unittest.TestCase):
    def test_after_host_skipping_taken(self):
        self.assertEqual(admin.next_candidates("192.168.50.10", 24, {"192.168.50.10", "192.168.50.12"}, span=4,
                                                 next_from="after"),
                         ["192.168.50.11", "192.168.50.13", "192.168.50.14"])

    def test_remembered_first_and_only_if_in_subnet(self):
        self.assertEqual(admin.next_candidates("192.168.50.10", 24, set(), "192.168.50.40", span=1, next_from="after"),
                         ["192.168.50.40", "192.168.50.11"])
        self.assertEqual(admin.next_candidates("192.168.50.10", 24, set(), "10.0.0.5", span=1, next_from="after"),
                         ["192.168.50.11"])
        self.assertEqual(admin.next_candidates("192.168.50.10", 24, {"192.168.50.40"}, "192.168.50.40", span=1,
                                                 next_from="after"),
                         ["192.168.50.11"])

    def test_stops_before_broadcast(self):
        self.assertEqual(admin.next_candidates("192.168.50.252", 24, set(), next_from="after"), ["192.168.50.253", "192.168.50.254"])


    def test_default_starts_just_after_host(self):
        self.assertEqual(admin.next_candidates("192.168.50.10", 24, set(), span=2), ["192.168.50.11", "192.168.50.12"])

    def test_next_from_200_then_after_host(self):
        c = admin.next_candidates("192.168.50.10", 24, {"192.168.50.201"}, span=3, next_from="200")
        self.assertEqual(c, ["192.168.50.200", "192.168.50.202", "192.168.50.11", "192.168.50.12", "192.168.50.13"])

    def test_next_from_forms(self):
        s = admin.next_start
        self.assertEqual(str(admin.ipaddress.ip_address(s("192.168.50.10", 24, "150"))), "192.168.50.150")
        self.assertEqual(str(admin.ipaddress.ip_address(s("10.0.5.10", 16, "200"))), "10.0.5.200")   # Pi's /24
        self.assertEqual(str(admin.ipaddress.ip_address(s("192.168.50.10", 24, "192.168.50.230"))), "192.168.50.230")
        self.assertIsNone(s("192.168.50.10", 24, "after"))
        self.assertIsNone(s("192.168.50.10", 24, "10.0.0.5"))        # outside the subnet
        self.assertIsNone(s("192.168.50.10", 26, "200"))             # .200 isn't in a /26 at .0
        self.assertEqual(admin.next_candidates("192.168.50.10", 26, set(), span=2, next_from="200"),
                         ["192.168.50.11", "192.168.50.12"])

    def test_stops_before_broadcast_from_start(self):
        self.assertEqual(admin.next_candidates("192.168.50.10", 24, set(), span=60, next_from="250")[:6],
                         ["192.168.50.250", "192.168.50.251", "192.168.50.252", "192.168.50.253",
                          "192.168.50.254", "192.168.50.11"])


class AddressManagerTest(unittest.TestCase):
    """keep_next / take_clash with the network calls replaced."""

    def setUp(self):
        import tempfile
        self.dir = tempfile.mkdtemp()
        self.saved = {k: getattr(admin, k) for k in ("arp_in_use", "arp_announce", "run", "interfaces",
                                                      "default_route", "CLAIMS_FILE", "save_claims", "load_claims")}
        self.saved_lease_dir = admin.slb.LEASE_DIR
        admin.slb.LEASE_DIR = self.dir
        claims = os.path.join(self.dir, "claims.json")
        admin.load_claims = lambda path=claims: self.saved["load_claims"](path)
        admin.save_claims = lambda c, path=claims: self.saved["save_claims"](c, path)
        self.busy = set()
        self.addrs = [{"ip": "192.168.50.10", "prefix": 24, "dhcp": True, "secondary": False}]
        self.calls = []
        admin.arp_in_use = lambda iface, ip, probes=3, wait=1.0: ip in self.busy
        admin.arp_announce = lambda iface, ip, count=2: None

        def fake_run(args, **kw):
            self.calls.append(args)
            if args[:3] == ["ip", "addr", "replace"]:
                self.addrs.append({"ip": args[3].split("/")[0], "prefix": 24, "dhcp": False, "secondary": True})
            return admin.subprocess.CompletedProcess(args, 0, "", "")
        admin.run = fake_run
        admin.interfaces = lambda: [{"name": "wlan0", "state": "UP", "addresses": list(self.addrs)}]
        admin.default_route = lambda: {"gateway": "192.168.50.1", "dev": "wlan0"}
        self.m = admin.AddressManager()

    def tearDown(self):
        for k, v in self.saved.items():
            setattr(admin, k, v)
        admin.slb.LEASE_DIR = self.saved_lease_dir

    def test_takes_next_free_and_remembers_it(self):
        self.busy = {"192.168.50.11"}
        self.m.keep_next("TSP143-2", "wlan0", "conn-1")
        self.assertEqual(admin.read_lease_file("TSP143-2")["ip"], "192.168.50.12")
        self.assertEqual(admin.read_lease_file("TSP143-2")["mode"], "next")
        self.assertIn(["ip", "addr", "replace", "192.168.50.12/24", "dev", "wlan0", "noprefixroute"], self.calls)
        self.assertEqual(admin.load_claims(), {"conn-1": {"TSP143-2": "192.168.50.12"}})
        # After a reboot (records gone), it asks for .12 again even though .11 is free now.
        os.unlink(os.path.join(self.dir, "lease-TSP143-2"))
        self.addrs = self.addrs[:1]
        self.busy = set()
        admin.AddressManager().keep_next("TSP143-2", "wlan0", "conn-1")
        self.assertEqual(admin.read_lease_file("TSP143-2")["ip"], "192.168.50.12")

    def test_never_moves_when_another_device_turns_up(self):
        self.m.keep_next("TSP143-2", "wlan0", "conn-1")
        self.assertEqual(admin.read_lease_file("TSP143-2")["ip"], "192.168.50.11")
        self.busy = {"192.168.50.11"}
        self.m.next_check["TSP143-2"] = 0
        with self.assertLogs(admin.log, "WARNING"):
            self.m.keep_next("TSP143-2", "wlan0", "conn-1")
        self.assertNotIn(["ip", "addr", "del", "192.168.50.11/24", "dev", "wlan0"], self.calls)
        self.assertEqual(admin.read_lease_file("TSP143-2")["ip"], "192.168.50.11")
        self.assertIn("Another device", self.m.notes["TSP143-2"])
        self.busy = set()
        self.m.next_check["TSP143-2"] = 0
        self.m.keep_next("TSP143-2", "wlan0", "conn-1")
        self.assertNotIn("TSP143-2", self.m.notes)

    def test_move_button_moves_only_on_request(self):
        self.m.keep_next("TSP143-2", "wlan0", "conn-1")
        with self.assertRaises(ValueError):
            self.m.request_move("TSP143-2")            # no conflict: nothing to fix
        self.busy = {"192.168.50.11"}
        self.m.next_check["TSP143-2"] = 0
        with self.assertLogs(admin.log, "WARNING"):
            self.m.keep_next("TSP143-2", "wlan0", "conn-1")
        self.m.request_move("TSP143-2")
        with self.assertLogs(admin.log, "WARNING"):
            self.m.keep_next("TSP143-2", "wlan0", "conn-1")
        self.assertIn(["ip", "addr", "del", "192.168.50.11/24", "dev", "wlan0"], self.calls)
        self.assertEqual(admin.read_lease_file("TSP143-2")["ip"], "192.168.50.12")
        self.assertEqual(admin.load_claims(), {"conn-1": {"TSP143-2": "192.168.50.12"}})
        self.assertNotIn("TSP143-2", self.m.conflict)

    def test_next_from_moves_the_start(self):
        self.m.next_from = "200"
        self.m.keep_next("TSP143-2", "wlan0", "conn-1")
        self.assertEqual(admin.read_lease_file("TSP143-2")["ip"], "192.168.50.200")

    def test_two_next_printers_get_different_addresses(self):
        self.m.keep_next("B", "wlan0", "conn-1")
        self.m.keep_next("C", "wlan0", "conn-1")
        self.assertEqual([admin.read_lease_file(n)["ip"] for n in "BC"], ["192.168.50.11", "192.168.50.12"])
        self.assertEqual(self.m.host_ip("wlan0"), ("192.168.50.10", 24))

    def test_clash_switches_to_next(self):
        with open(os.path.join(self.dir, "clash-TSP143-2"), "w") as f:
            f.write("ip=192.168.50.10\ninterface=wlan0\n")
        with self.assertLogs(admin.log, "WARNING"):
            self.assertTrue(self.m.take_clash("TSP143-2"))
        self.assertIn("TSP143-2", self.m.fallback)
        self.assertIn("next available address", self.m.notes["TSP143-2"])
        self.assertFalse(self.m.take_clash("TSP143-2"))


class MockPrinterTest(unittest.TestCase):
    def test_status_and_etb(self):
        import tempfile, time
        spec2 = importlib.util.spec_from_file_location("mock_printer", os.path.join(ROOT, "tools", "mock_printer.py"))
        mock = importlib.util.module_from_spec(spec2)
        spec2.loader.exec_module(mock)
        d = tempfile.mkdtemp()
        m = mock.MockPrinter(os.path.join(d, "P"), os.path.join(d, "jobs"))
        self.addCleanup(m.close)
        self.assertEqual(os.path.realpath(os.path.join(d, "P")), os.path.realpath(m.path))
        out = []
        m.write = out.append
        m.handle(b"\x1b\x06\x01")
        self.assertEqual(out[-1], bytes.fromhex("230600000000000000"))
        m.handle(b"x" * 100 + b"\x17")
        m.etb_at -= 1
        m.tick()
        self.assertEqual(out[-1][7], 1 << 1)
        m.paper_out = True
        self.assertEqual(m.frame()[5] & 0x08, 0x08)
        m.last_rx -= 5
        m.tick()
        self.assertEqual(len(os.listdir(os.path.join(d, "jobs"))), 1)


class LinkStateTest(unittest.TestCase):
    def test_only_rfcomm_devices_are_bluetooth(self):
        self.assertTrue(admin.is_bluetooth_device("/dev/rfcomm1"))
        self.assertFalse(admin.is_bluetooth_device("/dev/ttyUSB0"))
        self.assertFalse(admin.is_bluetooth_device("/home/pi/mock/TSP143-2"))

    def test_mock_printer_shows_connected_when_bridge_answers(self):
        saved = {k: getattr(admin, k) for k in ("printers", "rfcomm_states", "read_status_frame",
                                                 "wifi_networks", "default_route", "interfaces", "saved_wifi")}
        self.addCleanup(lambda: [setattr(admin, k, v) for k, v in saved.items()])
        admin.printers = lambda: [{"name": "M", "device": "/home/pi/mock/M", "local": "127.0.0.2",
                                   "address": "10.0.0.6", "mode": "next"}]
        admin.rfcomm_states = lambda: {}
        admin.wifi_networks = lambda rescan: []
        admin.default_route = lambda: {}
        admin.interfaces = lambda: []
        admin.saved_wifi = lambda: []
        admin.read_status_frame = lambda host, timeout=1.5: bytes.fromhex("2386000000000000000000")
        p = admin.status()["printers"][0]
        self.assertEqual((p["bluetooth"], p["status"]["state"]), ("connected", "ok"))
        admin.read_status_frame = lambda host, timeout=1.5: None
        self.assertEqual(admin.status()["printers"][0]["bluetooth"], "not connected")


class AddressLabelTest(unittest.TestCase):
    def test_owner_and_kind(self):
        import tempfile
        d = tempfile.mkdtemp()
        saved = admin.slb.LEASE_DIR
        admin.slb.LEASE_DIR = d
        self.addCleanup(setattr, admin.slb, "LEASE_DIR", saved)
        with open(os.path.join(d, "host"), "w") as f:
            f.write("ip=10.0.0.4\nprefix=24\ninterface=wlan0\n")
        ifaces = [{"name": "wlan0", "state": "UP", "addresses": [
            {"ip": "10.0.0.4", "prefix": 24, "dhcp": True, "secondary": False},
            {"ip": "10.0.0.6", "prefix": 24, "dhcp": False, "secondary": True},
            {"ip": "10.0.0.9", "prefix": 24, "dhcp": False, "secondary": True}]}]
        plist = [{"name": "P1", "address": "10.0.0.6", "mode": "next"},
                 {"name": "P0", "address": "10.0.0.4", "mode": "auto"}]
        a = admin.label_addresses(ifaces, plist)[0]["addresses"]
        self.assertEqual([(x["owner"], x["kind"]) for x in a],
                         [("this device, P0", "DHCP"), ("P1", "next available"), ("", "fixed")])


class SshctlPageTest(unittest.TestCase):
    def test_login_return_only_to_own_pages(self):
        self.assertIn('name="next" value="/sshctl"', admin.page_login("", "/sshctl"))
        for evil in ["https://evil.example/", "//evil.example", "/sshctl?x", "/api/ssh", ""]:
            self.assertIn('name="next" value="/"', admin.page_login("", evil))

    def test_page_is_not_linked_from_main_page(self):
        self.assertNotIn("sshctl", admin.APP_HTML)
        self.assertIn('id="ssh"', admin.SSHCTL_HTML)


if __name__ == "__main__":
    unittest.main()
