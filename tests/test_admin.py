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
        self.assertEqual(admin.nm_split("*:UNIFI:57:WPA2:64"), ["*", "UNIFI", "57", "WPA2", "64"])

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
        a = admin.client_id("dc:a6:32:25:92:a3", "TSP143-2")
        self.assertEqual(a, admin.client_id("DC:A6:32:25:92:A3", "TSP143-2"))
        self.assertNotEqual(a, admin.client_id("dc:a6:32:25:92:a3", "TSP143-3"))
        self.assertNotEqual(a, admin.client_id("dc:a6:32:00:00:01", "TSP143-2"))
        self.assertTrue(a.startswith("00"))
        self.assertEqual(bytes.fromhex(a[2:]).decode(), "star-lan-bridge:dca6322592a3:TSP143-2")

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
        lease = {"ip": "192.168.1.137", "mask": "24", "lease": "43200", "router": "192.168.1.1", "serverid": "192.168.1.1"}
        rc, calls = self.run_hook("bound", lease, d)
        self.assertEqual(rc, 0)
        self.assertIn("ip addr replace 192.168.1.137/24 dev wlan0 valid_lft 43200 preferred_lft 43200 noprefixroute", calls)
        with open(os.path.join(d, "lease-TSP143-2")) as f:
            rec = f.read()
        self.assertIn("ip=192.168.1.137", rec)
        self.assertIn("interface=wlan0", rec)

        rc, calls = self.run_hook("renew", {**lease, "ip": "192.168.1.140"}, d)
        self.assertIn("ip addr del 192.168.1.137/24 dev wlan0", calls)
        self.assertIn("ip addr replace 192.168.1.140/24", calls)

        rc, calls = self.run_hook("leasefail", {}, d)
        self.assertIn("ip addr del 192.168.1.140/24 dev wlan0", calls)
        self.assertFalse(os.path.exists(os.path.join(d, "lease-TSP143-2")))

    def test_rejects_bad_input(self):
        import tempfile
        d = tempfile.mkdtemp()
        rc, calls = self.run_hook("bound", {"ip": "1.2.3.4; reboot", "mask": "24"}, d)
        self.assertNotEqual(rc, 0)
        self.assertNotIn("ip addr", calls)
        rc, _ = self.run_hook("bound", {"ip": "1.2.3.4", "PRINTER": "../x"}, d)
        self.assertNotEqual(rc, 0)


if __name__ == "__main__":
    unittest.main()
