#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""star-admin: a small web page for setting up a star-lan-bridge host on site.

Open http://<hostname>.local (or the Pi's IP) from a phone or laptop on the same
network, or on an Ethernet cable plugged into the Pi, and you can:

  * see the Pi's network: Wi-Fi network, signal, every IP address, gateway;
  * see each printer: its IP address, Bluetooth link and paper/online status;
  * scan for Wi-Fi networks and join one (WPA2-Personal or open), either right
    away or saved for later, so the Pi moves to it when the current network is
    out of range;
  * forget saved networks.

Security
--------
* Nothing works until an admin password is set on the Pi itself:
      sudo python3 /opt/star-lan-bridge/star_admin.py --set-password
  The password is stored as a salted PBKDF2-SHA256 hash in
  /etc/star-admin/auth.json (root only).
* Sessions are random tokens in an HttpOnly, SameSite=Strict cookie, valid for
  12 hours. Logins are rate-limited per client address.
* Every state-changing request must be a JSON POST carrying the header
  "X-Star-Admin: 1". Browsers can't send that header cross-site without a CORS
  preflight, which this server never approves, so other sites can't drive it.
* The page is plain HTTP. Use it on the venue's staff network or a cable, not
  on open guest Wi-Fi. Wi-Fi passwords are written straight into a root-only
  NetworkManager keyfile, never put on a command line or in a log.

Runs as root (it changes NetworkManager connections). Python standard library
only; Linux with NetworkManager (nmcli) required.
"""
from __future__ import annotations

import argparse
import getpass
import hashlib
import hmac
import html
import http.server
import json
import logging
import os
import re
import secrets
import socket
import signal
import subprocess
import sys
import threading
import time
import uuid as uuidlib
from urllib.parse import parse_qs, urlsplit

log = logging.getLogger("star-admin")

# The bridge lives next to this file; reuse its config parser and address resolver.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import star_lan_bridge as slb  # noqa: E402

AUTH_FILE = "/etc/star-admin/auth.json"
BRIDGE_CONF = "/etc/star-lan-bridge.conf"
NM_DIR = "/etc/NetworkManager/system-connections"
WIFI_IFACE = "wlan0"
SESSION_TTL = 12 * 3600
PBKDF2_ITERATIONS = 240_000
MIN_PASSWORD = 10


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
def run(args: list[str], timeout: float = 20, env: dict | None = None) -> subprocess.CompletedProcess:
    """Run a command without a shell. Arguments are never interpolated into a shell."""
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                          env={**os.environ, **env} if env else None)


def nm_split(line: str) -> list[str]:
    """Split one line of `nmcli -t` output. Fields are ':'-separated; a literal
    ':' or '\\' inside a field is escaped with a backslash."""
    out, cur, esc = [], [], False
    for ch in line:
        if esc:
            cur.append(ch)
            esc = False
        elif ch == "\\":
            esc = True
        elif ch == ":":
            out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    out.append("".join(cur))
    return out


def validate_ssid(ssid: str) -> str:
    if not isinstance(ssid, str) or not ssid:
        raise ValueError("Enter the network name.")
    if len(ssid.encode()) > 32:
        raise ValueError("Network names are at most 32 bytes.")
    if any(ord(c) < 32 or ord(c) == 127 for c in ssid):
        raise ValueError("The network name contains a control character.")
    return ssid


def validate_psk(psk: str) -> str:
    """WPA2-Personal passphrase: 8-63 printable ASCII characters, or empty for an open network."""
    if not isinstance(psk, str):
        raise ValueError("Bad password field.")
    if psk == "":
        return psk
    if not 8 <= len(psk) <= 63 or any(not 32 <= ord(c) <= 126 for c in psk):
        raise ValueError("Wi-Fi passwords are 8 to 63 characters (letters, digits, symbols, spaces).")
    return psk


def keyfile_escape(value: str) -> str:
    """Escape a value for a NetworkManager keyfile (GLib key-file syntax)."""
    v = value.replace("\\", "\\\\").replace("\n", "\\n").replace("\t", "\\t").replace("\r", "\\r")
    if v.startswith(" "):
        v = "\\s" + v[1:]
    return v


def build_keyfile(ssid: str, psk: str, conn_uuid: str, hidden: bool, priority: int = 10) -> str:
    """Return a NetworkManager keyfile for a WPA2-Personal (or open) network using DHCP."""
    lines = [
        "[connection]",
        f"id={keyfile_escape(ssid)}",
        f"uuid={conn_uuid}",
        "type=wifi",
        f"interface-name={WIFI_IFACE}",
        "autoconnect=true",
        f"autoconnect-priority={priority}",
        "",
        "[wifi]",
        "mode=infrastructure",
        f"ssid={keyfile_escape(ssid)}",
    ]
    if hidden:
        lines.append("hidden=true")
    if psk:
        lines += ["", "[wifi-security]", "key-mgmt=wpa-psk", f"psk={keyfile_escape(psk)}"]
    lines += ["", "[ipv4]", "method=auto", "", "[ipv6]", "method=auto", ""]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Admin password
# ---------------------------------------------------------------------------
def hash_password(password: str, salt: bytes | None = None, iterations: int = PBKDF2_ITERATIONS) -> dict:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return {"alg": "pbkdf2-sha256", "iterations": iterations, "salt": salt.hex(), "hash": digest.hex()}


def check_password(password: str, record: dict) -> bool:
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(record["salt"]),
                                 int(record["iterations"]))
    return hmac.compare_digest(digest.hex(), record["hash"])


def load_auth(path: str = AUTH_FILE) -> dict | None:
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def set_password_interactive(path: str = AUTH_FILE) -> int:
    pw = getpass.getpass("New admin password: ")
    if len(pw) < MIN_PASSWORD:
        print(f"Use at least {MIN_PASSWORD} characters.")
        return 1
    if getpass.getpass("Repeat it: ") != pw:
        print("The passwords don't match.")
        return 1
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(hash_password(pw), f)
    os.replace(tmp, path)
    print(f"Saved to {path}. Restart star-admin if it's running: sudo systemctl restart star-admin")
    return 0


# ---------------------------------------------------------------------------
# Network and printer status
# ---------------------------------------------------------------------------
def ip_json(*args: str):
    r = run(["ip", "-j", *args])
    try:
        return json.loads(r.stdout or "[]")
    except ValueError:
        return []


def interfaces() -> list[dict]:
    """IPv4 addresses on every non-loopback interface, with link state."""
    out = []
    for link in ip_json("-4", "addr", "show"):
        name = link.get("ifname", "")
        if name == "lo":
            continue
        addrs = [{"ip": a["local"], "prefix": a.get("prefixlen"),
                  "dhcp": bool(a.get("dynamic")), "secondary": bool(a.get("secondary"))}
                 for a in link.get("addr_info", []) if a.get("family") == "inet"]
        out.append({"name": name, "state": link.get("operstate", "UNKNOWN"), "addresses": addrs})
    return out


def default_route() -> dict:
    routes = ip_json("route", "show", "default")
    routes.sort(key=lambda r: r.get("metric", 0))
    return routes[0] if routes else {}


def wifi_networks(rescan: bool) -> list[dict]:
    """Visible networks, strongest first, one entry per SSID."""
    r = run(["nmcli", "-t", "-f", "IN-USE,SSID,SIGNAL,SECURITY,CHAN", "dev", "wifi", "list",
             "ifname", WIFI_IFACE, "--rescan", "yes" if rescan else "no"], timeout=30)
    best: dict[str, dict] = {}
    for line in r.stdout.splitlines():
        f = nm_split(line)
        if len(f) < 5 or not f[1]:
            continue  # hidden networks have no SSID to show
        try:
            signal = int(f[2])
        except ValueError:
            signal = 0
        net = {"ssid": f[1], "signal": signal, "security": f[3] or "open",
               "channel": f[4], "in_use": f[0] == "*"}
        old = best.get(net["ssid"])
        if not old or net["in_use"] or (signal > old["signal"] and not old["in_use"]):
            if old and old["in_use"]:
                net["in_use"] = True
            best[net["ssid"]] = net
    return sorted(best.values(), key=lambda n: (not n["in_use"], -n["signal"]))


def saved_wifi() -> list[dict]:
    r = run(["nmcli", "-t", "-f", "UUID,NAME,TYPE,AUTOCONNECT,AUTOCONNECT-PRIORITY,DEVICE", "con", "show"])
    out = []
    for line in r.stdout.splitlines():
        f = nm_split(line)
        if len(f) >= 6 and f[2] == "802-11-wireless":
            out.append({"uuid": f[0], "name": f[1], "autoconnect": f[3] == "yes",
                        "priority": f[4], "active": bool(f[5])})
    return out


def rfcomm_states() -> dict[str, str]:
    """{'/dev/rfcomm0': 'connected', ...} from the `rfcomm` tool."""
    r = run(["rfcomm"])
    out = {}
    for line in r.stdout.splitlines():
        m = re.match(r"(rfcomm\d+):\s+(\S+)\s+channel\s+\d+\s+(\S+)", line)
        if m:
            out["/dev/" + m.group(1)] = {"mac": m.group(2), "state": m.group(3)}
    return out


# Kernel RFCOMM states as shown on the page.
BT_LABELS = {"connected": "connected", "config": "connecting", "connect": "connecting",
             "closed": "not connected", "clean": "not connected", "bound": "not connected"}


def read_status_frame(host: str, timeout: float = 1.5) -> bytes | None:
    """Ask the bridge for a printer's status over TCP 9101, exactly as a POS does."""
    try:
        with socket.create_connection((host, 9101), timeout=timeout) as c:
            c.sendall(b"\x32" + b"\0" * 50)
            c.settimeout(timeout)
            data = b""
            while len(data) < 22:
                chunk = c.recv(64)
                if not chunk:
                    break
                data += chunk
            return data[:11] if len(data) >= 11 else None
    except OSError:
        return None


def describe_status(frame: bytes | None) -> dict:
    """Plain-language status from an 11-byte LAN ASB frame.

    Paper-empty and offline use bit 3 of bytes 5 and 2 (seen on a TSP143IIIBI
    with no paper: 23 86 08 00 00 08 ...). Anything else non-zero apart from the
    ETB counter (byte 7) is reported as needing attention, with the raw bytes.
    """
    if not frame:
        return {"state": "unknown", "text": "No answer from the bridge", "raw": ""}
    raw = frame.hex(" ")
    if frame[5] & 0x08:
        return {"state": "warn", "text": "Out of paper", "raw": raw}
    if frame[2] & 0x08:
        return {"state": "warn", "text": "Offline (cover open or error)", "raw": raw}
    rest = bytes(frame[2:7]) + bytes(frame[8:9])
    if any(rest):
        return {"state": "warn", "text": "Needs attention", "raw": raw}
    return {"state": "ok", "text": "Ready", "raw": raw}


def read_lease_file(name: str) -> dict:
    """The lease record star-dhcp-alias.sh keeps for a printer, or {}."""
    out = {}
    try:
        with open(os.path.join(slb.LEASE_DIR, f"lease-{name}")) as f:
            for line in f:
                k, _, v = line.strip().partition("=")
                out[k] = v
    except OSError:
        pass
    return out


def printers() -> list[dict]:
    """Printers from the bridge config with their current addresses."""
    try:
        plist = slb.read_printer_config(BRIDGE_CONF)
    except (OSError, ValueError):
        plist = [{"name": "printer", "device": "/dev/rfcomm0", "address": "auto", "local": "127.0.0.1", "mac": None}]
    out = []
    for p in plist:
        mode = p["address"].lower()
        resolved = slb.resolve_address(p["name"], p["address"])
        info = {"name": p["name"], "device": p["device"], "local": p["local"] or "",
                "address": resolved or "", "mode": mode if mode in ("auto", "dhcp") else "fixed"}
        if mode == "dhcp":
            lease = read_lease_file(p["name"])
            if lease.get("obtained") and lease.get("lease"):
                try:
                    info["lease_expires"] = int(lease["obtained"]) + int(lease["lease"])
                except ValueError:
                    pass
        out.append(info)
    return out


def status() -> dict:
    route = default_route()
    links = rfcomm_states()
    plist = []
    for p in printers():
        frame = read_status_frame(p["local"] or p["address"] or "127.0.0.1")
        link = links.get(p["device"], {})
        bt = BT_LABELS.get(link.get("state", ""), "not set up")
        st = describe_status(frame)
        if bt != "connected" and st["state"] != "unknown":
            # The bridge marks the cached status offline while the link is down;
            # say why in plain words instead of "cover open".
            st = {**st, "state": "bad", "text": "Switched off or out of Bluetooth range"}
        plist.append({**p, "bluetooth": bt, "bt_mac": link.get("mac", ""), "status": st})
    wifi = next((n for n in wifi_networks(rescan=False) if n["in_use"]), None)
    return {
        "hostname": socket.gethostname(),
        "mdns": socket.gethostname() + ".local",
        "wifi": wifi,
        "gateway": route.get("gateway", ""),
        "route_dev": route.get("dev", ""),
        "interfaces": interfaces(),
        "printers": plist,
        "saved": saved_wifi(),
        "join": JOBS.snapshot(),
    }


# ---------------------------------------------------------------------------
# Joining Wi-Fi
# ---------------------------------------------------------------------------
class JoinJobs:
    """The most recent join attempt, run in the background so the HTTP response
    goes out before the Pi (and maybe the browser) changes network."""

    def __init__(self):
        self.lock = threading.Lock()
        self.last: dict = {}

    def snapshot(self) -> dict:
        with self.lock:
            return dict(self.last)

    def set(self, **kw):
        with self.lock:
            self.last = {**kw, "time": time.time()}


JOBS = JoinJobs()


def find_saved(ssid: str) -> list[str]:
    return [s["uuid"] for s in saved_wifi() if s["name"] == ssid]


def add_network(ssid: str, psk: str, hidden: bool) -> str:
    """Write a keyfile for the network (replacing an older profile with the same
    name) and load it into NetworkManager. Returns the new connection UUID."""
    for old in find_saved(ssid):
        run(["nmcli", "con", "delete", "uuid", old])
    conn_uuid = str(uuidlib.uuid4())
    path = os.path.join(NM_DIR, f"star-admin-{conn_uuid}.nmconnection")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(build_keyfile(ssid, psk, conn_uuid, hidden))
    r = run(["nmcli", "con", "load", path])
    if r.returncode != 0:
        os.unlink(path)
        raise RuntimeError("NetworkManager rejected the network settings: " + (r.stderr.strip() or "unknown error"))
    return conn_uuid


def join_worker(ssid: str, conn_uuid: str, previous: str | None):
    JOBS.set(ssid=ssid, state="connecting", text=f"Connecting to {ssid}…")
    r = run(["nmcli", "--wait", "45", "con", "up", "uuid", conn_uuid, "ifname", WIFI_IFACE], timeout=60)
    if r.returncode == 0:
        time.sleep(3)
        addrs = [a["ip"] for i in interfaces() if i["name"] == WIFI_IFACE for a in i["addresses"]]
        JOBS.set(ssid=ssid, state="ok", text=f"Connected to {ssid}", addresses=addrs)
        log.info("joined %s, addresses %s", ssid, addrs)
        return
    reason = (r.stderr or r.stdout).strip().splitlines()[-1:] or ["unknown error"]
    log.warning("join %s failed: %s", ssid, reason[0])
    # A wrong password would otherwise leave a profile that keeps failing; remove it
    # and go back to the network we were on.
    run(["nmcli", "con", "delete", "uuid", conn_uuid])
    if previous:
        run(["nmcli", "--wait", "30", "con", "up", "uuid", previous], timeout=45)
    JOBS.set(ssid=ssid, state="error",
             text=f"Couldn't connect to {ssid}. Check the password and that the network is in range. "
                  "The Pi went back to its previous network.", detail=reason[0])


def active_wifi_uuid() -> str | None:
    return next((s["uuid"] for s in saved_wifi() if s["active"]), None)


# ---------------------------------------------------------------------------
# One DHCP lease per printer
# ---------------------------------------------------------------------------
ALIAS_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "star-dhcp-alias.sh")


def client_id(mac: str, printer: str) -> str:
    """DHCP client identifier (option 61) for a printer, as udhcpc hex.

    Type 0 (free-form) followed by "star-lan-bridge:<host MAC>:<printer>". The
    host MAC keeps two Pis with the same printer names from sharing a lease;
    a fixed value per printer means the router hands back the same address.
    """
    text = f"star-lan-bridge:{mac.replace(':', '').lower()}:{printer}"
    return "00" + text.encode()[:60].hex()


def dhcp_hostname(printer: str) -> str:
    name = f"{socket.gethostname()}-{printer}".lower()
    return re.sub(r"[^a-z0-9-]", "-", name)[:63].strip("-")


def iface_mac(iface: str) -> str:
    try:
        with open(f"/sys/class/net/{iface}/address") as f:
            return f.read().strip()
    except OSError:
        return "00:00:00:00:00:00"


def active_connection(iface: str) -> str | None:
    r = run(["nmcli", "-t", "-f", "UUID,DEVICE", "con", "show", "--active"])
    for line in r.stdout.splitlines():
        f = nm_split(line)
        if len(f) >= 2 and f[1] == iface:
            return f[0]
    return None


class DhcpManager:
    """Keeps one `busybox udhcpc` per printer with `address = dhcp`.

    udhcpc requests, renews and (with star-dhcp-alias.sh) adds/removes the
    address. This class only starts and stops those clients: on the interface
    that carries the default route, restarting them when the Pi moves to a
    different network so each printer gets an address on the new one. The
    bridge notices address changes by itself and restarts.
    """

    def __init__(self):
        self.procs: dict[str, subprocess.Popen] = {}
        self.netkey: tuple | None = None
        self.next_start: dict[str, float] = {}

    def run_forever(self):
        os.makedirs(slb.LEASE_DIR, mode=0o755, exist_ok=True)
        while True:
            try:
                self.tick()
            except Exception:
                log.exception("dhcp: tick failed")
            time.sleep(5)

    def wanted(self) -> list[str]:
        try:
            return [p["name"] for p in slb.read_printer_config(BRIDGE_CONF) if p["address"].lower() == "dhcp"]
        except (OSError, ValueError):
            return []

    def tick(self):
        iface = slb.default_route_iface()
        conn = active_connection(iface) if iface else None
        key = (iface, conn) if iface and conn else None
        if key != self.netkey:
            if self.netkey is not None:
                log.info("dhcp: network changed %s -> %s; renewing printer addresses", self.netkey, key)
                self.stop_all(drop_addresses=True)
            elif key is not None:
                self.drop_stale(iface)  # leases from another interface after a restart
            self.netkey = key
        if key is None:
            return
        names = self.wanted()
        for name in list(self.procs):
            if name not in names:
                self.stop(name, drop_address=True)
        for name in names:
            proc = self.procs.get(name)
            if proc and proc.poll() is None:
                continue
            if proc:
                log.warning("dhcp: client for %s exited (%s); restarting", name, proc.returncode)
                self.procs.pop(name)
                self.next_start[name] = time.time() + 10
            if time.time() >= self.next_start.get(name, 0):
                self.start(name, iface)
        self.ensure_addresses(iface, names)

    def start(self, name: str, iface: str):
        args = ["busybox", "udhcpc", "-i", iface, "-f", "-C",
                "-x", f"0x3d:{client_id(iface_mac(iface), name)}",
                "-x", f"hostname:{dhcp_hostname(name)}",
                "-s", ALIAS_SCRIPT, "-t", "5", "-T", "3", "-A", "20"]
        prev = read_lease_file(name)
        if prev.get("ip") and prev.get("interface") == iface:
            args += ["-r", prev["ip"]]  # ask for the address we had
        env = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "PRINTER": name, "LEASE_DIR": slb.LEASE_DIR}
        self.procs[name] = subprocess.Popen(args, env=env, stdin=subprocess.DEVNULL,
                                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        log.info("dhcp: requesting an address for %s on %s", name, iface)

    def stop(self, name: str, drop_address: bool):
        proc = self.procs.pop(name, None)
        if proc and proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(5)
            except subprocess.TimeoutExpired:
                proc.kill()
        if drop_address:
            self.drop_lease(name)

    def stop_all(self, drop_addresses: bool):
        for name in list(self.procs):
            self.stop(name, drop_addresses)
        if drop_addresses:
            for f in os.listdir(slb.LEASE_DIR) if os.path.isdir(slb.LEASE_DIR) else []:
                if f.startswith("lease-"):
                    self.drop_lease(f[len("lease-"):])

    def drop_lease(self, name: str):
        lease = read_lease_file(name)
        if lease.get("ip") and lease.get("interface"):
            run(["ip", "addr", "del", f"{lease['ip']}/{lease.get('mask', '24')}", "dev", lease["interface"]])
        try:
            os.unlink(os.path.join(slb.LEASE_DIR, f"lease-{name}"))
        except OSError:
            pass

    def drop_stale(self, iface: str):
        for f in os.listdir(slb.LEASE_DIR) if os.path.isdir(slb.LEASE_DIR) else []:
            if f.startswith("lease-") and read_lease_file(f[6:]).get("interface") != iface:
                self.drop_lease(f[6:])

    def ensure_addresses(self, iface: str, names: list[str]):
        """Put a leased address back if something (e.g. NetworkManager) removed it."""
        have = {a["ip"] for i in interfaces() if i["name"] == iface for a in i["addresses"]}
        for name in names:
            lease = read_lease_file(name)
            ip_ = lease.get("ip")
            if not ip_ or ip_ in have or lease.get("interface") != iface:
                continue
            try:
                left = int(lease["obtained"]) + int(lease["lease"]) - int(time.time())
            except (KeyError, ValueError):
                continue
            if left > 0:
                log.warning("dhcp: %s lost its address %s; re-adding", name, ip_)
                run(["ip", "addr", "replace", f"{ip_}/{lease.get('mask', '24')}", "dev", iface,
                     "valid_lft", str(left), "preferred_lft", str(left), "noprefixroute"])


DHCP = DhcpManager()


# ---------------------------------------------------------------------------
# SSH on/off (hidden /sshctl page)
# ---------------------------------------------------------------------------
# Debian runs sshd as ssh.service; ssh.socket is the socket-activated
# alternative, kept off so it can't bring SSH back on its own.
SSH_SERVICE, SSH_SOCKET = "ssh.service", "ssh.socket"
LOGIN_TARGETS = ("/", "/sshctl")


def ssh_state() -> dict:
    def q(cmd, unit):
        return run(["systemctl", cmd, unit]).stdout.strip()
    enabled = q("is-enabled", SSH_SERVICE) == "enabled" or q("is-enabled", SSH_SOCKET) == "enabled"
    running = q("is-active", SSH_SERVICE) == "active" or q("is-active", SSH_SOCKET) == "active"
    return {"enabled": enabled, "running": running}


def apply_ssh(on: bool) -> dict:
    """Turn new SSH logins on or off, now and at boot. Sessions already open
    are not ended. Raspberry Pi OS also turns SSH on at boot if a file named
    `ssh` is put on the boot partition (sshswitch.service): the way back in if
    SSH is off and this page can't be reached."""
    # Skip Debian's SysV init-script mirroring (systemd-sysv-install): it edits
    # /etc/rc*.d, which is read-only in this service's sandbox, and those links
    # are ignored anyway because ssh has a native systemd unit.
    env = {"SYSTEMCTL_SKIP_SYSV": "1"}
    if on:
        r = run(["systemctl", "enable", "--now", SSH_SERVICE], timeout=30, env=env)
    else:
        run(["systemctl", "disable", "--now", SSH_SOCKET], timeout=30, env=env)
        r = run(["systemctl", "disable", "--now", SSH_SERVICE], timeout=30, env=env)
    if r.returncode != 0:
        log.warning("ssh: systemctl failed: %s", (r.stderr or r.stdout).strip()[-300:])
    state = ssh_state()
    log.warning("ssh: now %s", "on" if state["running"] else "off")
    return state


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
class Sessions:
    def __init__(self):
        self.lock = threading.Lock()
        self.tokens: dict[str, float] = {}
        self.failures: dict[str, list[float]] = {}

    def new(self) -> str:
        tok = secrets.token_urlsafe(32)
        with self.lock:
            self.tokens[tok] = time.time() + SESSION_TTL
        return tok

    def valid(self, tok: str | None) -> bool:
        if not tok:
            return False
        with self.lock:
            exp = self.tokens.get(tok)
            if exp and exp > time.time():
                return True
            self.tokens.pop(tok, None)
            return False

    def drop(self, tok: str | None):
        with self.lock:
            self.tokens.pop(tok or "", None)

    def throttled(self, client: str) -> bool:
        """At most 5 failed logins per client per 5 minutes."""
        now = time.time()
        with self.lock:
            recent = [t for t in self.failures.get(client, []) if now - t < 300]
            self.failures[client] = recent
            return len(recent) >= 5

    def failed(self, client: str):
        with self.lock:
            self.failures.setdefault(client, []).append(time.time())


SESSIONS = Sessions()


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "star-admin"
    sys_version = ""

    # -- plumbing --
    def log_message(self, fmt, *args):
        # Skip routine page loads and the page's 5-second status refresh; log
        # sign-ins, changes and anything that failed.
        msg = fmt % args
        if self.command == "GET" and '" 200 ' in msg:
            return
        log.info("%s %s", self.client_address[0], msg)

    def cookie_token(self) -> str | None:
        for part in self.headers.get("Cookie", "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == "star_admin":
                return v
        return None

    def send(self, code: int, body: bytes, ctype: str, extra: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # Never let a browser (or its back/forward cache) show old addresses.
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; style-src 'self' 'unsafe-inline'; "
                         "script-src 'self' 'unsafe-inline'; frame-ancestors 'none'")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def json(self, code: int, obj):
        self.send(code, json.dumps(obj).encode(), "application/json")

    def redirect(self, where: str, extra: dict | None = None):
        self.send(303, b"", "text/plain", {"Location": where, **(extra or {})})

    def body(self, limit: int = 8192) -> bytes:
        n = int(self.headers.get("Content-Length", "0") or 0)
        if n > limit:
            raise ValueError("request too large")
        return self.rfile.read(n)

    def authed(self) -> bool:
        return SESSIONS.valid(self.cookie_token())

    # -- routes --
    def route(self) -> str:
        """Request path without the query string (the page adds ?t=... to dodge caches)."""
        return urlsplit(self.path).path

    def do_GET(self):
        path = self.route()
        if path == "/":
            if not load_auth():
                return self.send(503, page_no_password().encode(), "text/html; charset=utf-8")
            if not self.authed():
                return self.send(200, page_login("").encode(), "text/html; charset=utf-8")
            return self.send(200, page_app().encode(), "text/html; charset=utf-8")
        if path == "/api/status":
            if not self.authed():
                return self.json(401, {"error": "Sign in again."})
            return self.json(200, status())
        # Not linked from anywhere; still needs the admin sign-in.
        if path == "/sshctl":
            if not load_auth():
                return self.send(503, page_no_password().encode(), "text/html; charset=utf-8")
            if not self.authed():
                return self.send(200, page_login("", "/sshctl").encode(), "text/html; charset=utf-8")
            return self.send(200, page_sshctl().encode(), "text/html; charset=utf-8")
        if path == "/api/ssh":
            if not self.authed():
                return self.json(401, {"error": "Sign in again."})
            return self.json(200, ssh_state())
        self.send(404, b"Not found", "text/plain")

    def do_POST(self):
        client = self.client_address[0]
        path = self.route()
        try:
            if path == "/login":
                return self.login(client)
            if not self.authed():
                return self.json(401, {"error": "Sign in again."})
            if self.headers.get("X-Star-Admin") != "1":
                return self.json(403, {"error": "Missing request header."})
            data = json.loads(self.body() or b"{}")
            if path == "/logout":
                SESSIONS.drop(self.cookie_token())
                return self.json(200, {"ok": True})
            if path == "/api/scan":
                return self.json(200, {"networks": wifi_networks(rescan=True)})
            if path == "/api/join":
                return self.join(data)
            if path == "/api/forget":
                return self.forget(data)
            if path == "/api/ssh":
                return self.set_ssh(data, client)
            self.json(404, {"error": "Not found."})
        except ValueError as e:
            self.json(400, {"error": str(e)})
        except Exception as e:  # keep the page usable and log the details
            log.exception("request failed")
            self.json(500, {"error": f"Something went wrong on the Pi: {e}"})

    def login(self, client: str):
        auth = load_auth()
        if not auth:
            return self.redirect("/")
        if SESSIONS.throttled(client):
            return self.send(429, page_login("Too many attempts. Wait five minutes and try again.").encode(),
                             "text/html; charset=utf-8")
        form = parse_qs(self.body().decode("utf-8", "replace"))
        nxt = form.get("next", ["/"])[0]
        nxt = nxt if nxt in LOGIN_TARGETS else "/"   # only our own pages: no open redirect
        if check_password(form.get("password", [""])[0], auth):
            tok = SESSIONS.new()
            return self.redirect(nxt, {"Set-Cookie": f"star_admin={tok}; HttpOnly; SameSite=Strict; Path=/; "
                                                     f"Max-Age={SESSION_TTL}"})
        SESSIONS.failed(client)
        time.sleep(1)
        self.send(401, page_login("That password isn't right.", nxt).encode(), "text/html; charset=utf-8")

    def join(self, data: dict):
        ssid = validate_ssid(data.get("ssid", ""))
        psk = validate_psk(data.get("password", ""))
        hidden = bool(data.get("hidden"))
        now = bool(data.get("connect_now", True))
        if JOBS.snapshot().get("state") == "connecting":
            raise ValueError("Already connecting to a network. Wait for it to finish.")
        previous = active_wifi_uuid()
        conn_uuid = add_network(ssid, psk, hidden)
        log.info("saved network %s (%s), connect now: %s", ssid, conn_uuid, now)
        if not now:
            JOBS.set(ssid=ssid, state="saved", text=f"Saved {ssid}. The Pi joins it automatically when it's in range.")
            return self.json(200, {"ok": True, "state": "saved"})
        threading.Thread(target=join_worker, args=(ssid, conn_uuid, previous), daemon=True).start()
        self.json(200, {"ok": True, "state": "connecting"})

    def set_ssh(self, data: dict, client: str):
        if not isinstance(data.get("enabled"), bool):
            raise ValueError("Send enabled: true or false.")
        want = data["enabled"]
        log.warning("ssh: %s requested by %s", "enable" if want else "disable", client)
        state = apply_ssh(want)
        if state["enabled"] != want or state["running"] != want:
            return self.json(500, {"error": "SSH didn't change as requested; see the star-admin log.", **state})
        self.json(200, state)

    def forget(self, data: dict):
        conn_uuid = str(data.get("uuid", ""))
        match = [s for s in saved_wifi() if s["uuid"] == conn_uuid]
        if not match:
            raise ValueError("That network isn't saved.")
        if match[0]["active"]:
            raise ValueError("That's the network the Pi is using now. Join another network first.")
        run(["nmcli", "con", "delete", "uuid", conn_uuid])
        self.json(200, {"ok": True})


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------
CSS = """
:root{--bg:#f4f6f8;--card:#fff;--ink:#17202a;--muted:#5b6776;--rule:#dfe4ea;--accent:#1d5f8a;--accent-ink:#fff;
--ok:#1d7a4e;--ok-bg:#e3f3ea;--warn:#995a00;--warn-bg:#fbefd9;--bad:#b03a2e;--bad-bg:#f8e3e0;
--mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;--sans:-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
@media (prefers-color-scheme:dark){:root{--bg:#101418;--card:#181e24;--ink:#e3e8ee;--muted:#98a4b2;--rule:#2b333c;
--accent:#7db5db;--accent-ink:#0d1820;--ok:#6cc59a;--ok-bg:#16291f;--warn:#e3b064;--warn-bg:#2d2416;--bad:#ef8a7e;--bad-bg:#321b18;color-scheme:dark}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 var(--sans);
padding:16px max(16px,env(safe-area-inset-left)) 48px}main{max-width:760px;margin:0 auto;display:grid;gap:16px}
h1{font-size:1.35rem;margin:8px 0 0}h2{font-size:1rem;margin:0 0 10px}.sub{color:var(--muted);margin:0}
.card{background:var(--card);border:1px solid var(--rule);border-radius:10px;padding:16px}
.kv{display:grid;grid-template-columns:max-content 1fr;gap:4px 16px}.kv dt{color:var(--muted)}.kv dd{margin:0;min-width:0;overflow-wrap:anywhere}
.mono{font-family:var(--mono);font-size:.9em}.ip{font-family:var(--mono);font-size:1.05rem;font-weight:600}
.pill{display:inline-block;padding:2px 9px;border-radius:999px;font-size:.8rem;font-weight:600}
.ok{background:var(--ok-bg);color:var(--ok)}.warn{background:var(--warn-bg);color:var(--warn)}.bad{background:var(--bad-bg);color:var(--bad)}
.unknown{background:var(--rule);color:var(--muted)}
.printers{display:grid;gap:12px;grid-template-columns:repeat(auto-fit,minmax(220px,1fr))}
.printer{border:1px solid var(--rule);border-radius:8px;padding:12px}
button,.btn{font:inherit;font-weight:600;border-radius:8px;border:1px solid var(--accent);background:var(--accent);color:var(--accent-ink);
padding:8px 14px;cursor:pointer}button.ghost{background:transparent;color:var(--accent)}button:disabled{opacity:.5;cursor:default}
button:focus-visible,input:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
input[type=text],input[type=password]{font:inherit;width:100%;padding:9px 10px;border:1px solid var(--rule);border-radius:8px;background:var(--bg);color:var(--ink)}
label{display:block;font-weight:600;margin:10px 0 4px}.row{display:flex;gap:10px;flex-wrap:wrap;align-items:center}
.nets{list-style:none;margin:10px 0 0;padding:0;border-top:1px solid var(--rule)}
.nets li{display:flex;gap:10px;align-items:center;padding:9px 2px;border-bottom:1px solid var(--rule);cursor:pointer}
.nets li:hover{background:var(--bg)}.nets .name{flex:1;min-width:0;overflow-wrap:anywhere;font-weight:600}
.bars{font-family:var(--mono);color:var(--muted);font-size:.85rem;white-space:nowrap}
.msg{border-radius:8px;padding:10px 12px;margin-top:12px}.check{display:flex;gap:8px;align-items:center;font-weight:400;margin:8px 0 0}
small{color:var(--muted)}table{width:100%;border-collapse:collapse}td{padding:6px 4px;border-bottom:1px solid var(--rule)}
"""


def shell(title: str, body: str) -> str:
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">'
            f"<title>{html.escape(title)}</title><style>{CSS}</style></head><body><main>{body}</main></body></html>")


def page_no_password() -> str:
    return shell("Printer bridge setup", """
<h1>Printer bridge setup</h1>
<div class="card"><h2>Set an admin password first</h2>
<p>This page stays locked until a password is set on the Pi. Over SSH or on a keyboard attached to the Pi, run:</p>
<p class="mono">sudo python3 /opt/star-lan-bridge/star_admin.py --set-password</p>
<p>Then reload this page.</p></div>""")


def page_login(error: str, next_path: str = "/") -> str:
    err = f'<div class="msg bad">{html.escape(error)}</div>' if error else ""
    nxt = next_path if next_path in LOGIN_TARGETS else "/"
    return shell("Sign in · printer bridge", f"""
<h1>Printer bridge</h1><p class="sub">{html.escape(socket.gethostname())}.local</p>
<form class="card" method="post" action="/login">
<input type="hidden" name="next" value="{html.escape(nxt)}">
<label for="password">Admin password</label>
<input id="password" name="password" type="password" autocomplete="current-password" required autofocus>
<div class="row" style="margin-top:14px"><button type="submit">Sign in</button></div>{err}
</form>""")


def page_sshctl() -> str:
    return shell("SSH access · printer bridge", SSHCTL_HTML.replace("{{HOST}}", html.escape(socket.gethostname())))


SSHCTL_HTML = """
<div class="row" style="justify-content:space-between">
  <div><h1>SSH access</h1><p class="sub mono">{{HOST}}.local</p></div>
  <a class="btn" href="/" style="text-decoration:none">Back</a>
</div>
<section class="card">
  <label class="check" style="font-weight:600;font-size:1.05rem">
    <input type="checkbox" id="ssh" disabled> Allow SSH logins
  </label>
  <p role="status" style="font-weight:600">SSH Connections: <span id="state" class="pill unknown">Checking…</span></p>
  <p><small>Turning SSH off stops new logins now and after reboots; sessions already open stay
  connected until they end. To get SSH back without this page, put a file named <span class="mono">ssh</span>
  on the SD card's boot partition and restart the Pi.</small></p>
  <div id="msg"></div>
</section>
<script>
const H = {"Content-Type":"application/json","X-Star-Admin":"1"};
const box = document.getElementById("ssh"), state = document.getElementById("state"), msg = document.getElementById("msg");
function show(s) {
  box.checked = s.enabled; box.disabled = false;
  state.textContent = s.running ? "Accepted" : "Denied";
  state.className = "pill " + (s.running ? "ok" : "bad");
}
async function load() {
  const r = await fetch("/api/ssh?t=" + Date.now(), {credentials:"same-origin", cache:"no-store"});
  if (r.status === 401) return location.reload();
  show(await r.json());
}
box.onchange = async () => {
  const want = box.checked;
  if (!want && !confirm("Turn SSH off? New SSH logins will be refused until you turn it back on here.")) {
    box.checked = true; return;
  }
  box.disabled = true; msg.innerHTML = "";
  // Pending badge while systemctl works (a few seconds, mostly when turning off).
  state.textContent = want ? "Turning on…" : "Turning off…";
  state.className = "pill warn";
  try {
    const r = await fetch("/api/ssh", {method:"POST", headers:H, credentials:"same-origin", body:JSON.stringify({enabled:want})});
    if (r.status === 401) return location.reload();
    const j = await r.json();
    if (!r.ok) msg.innerHTML = '<div class="msg bad"></div>', msg.firstChild.textContent = j.error || ("Error " + r.status);
    show(j.enabled === undefined ? {enabled:!want, running:!want} : j);
  } catch (e) { msg.innerHTML = '<div class="msg bad">Couldn’t reach the Pi.</div>'; box.disabled = false; }
};
load();
</script>
"""


def page_app() -> str:
    return shell("Printer bridge", APP_HTML.replace("{{HOST}}", html.escape(socket.gethostname())))


APP_HTML = """
<div class="row" style="justify-content:space-between">
  <div><h1>Printer bridge</h1><p class="sub mono">{{HOST}}.local</p></div>
  <button class="ghost" id="logout" type="button">Sign out</button>
</div>
<p class="sub" id="updated" role="status">Loading…</p>

<section class="card" aria-labelledby="h-net">
  <h2 id="h-net">Network</h2>
  <dl class="kv" id="net"><dt>Loading</dt><dd>…</dd></dl>
</section>

<section class="card" aria-labelledby="h-pr">
  <h2 id="h-pr">Printers</h2>
  <p class="sub">Point the POS at these addresses (search for Star LAN printers).</p>
  <div class="printers" id="printers" style="margin-top:10px"></div>
</section>

<section class="card" aria-labelledby="h-join">
  <h2 id="h-join">Join a Wi-Fi network</h2>
  <div class="row"><button type="button" id="scan">Scan for networks</button><small id="scan-note">Tap a network to fill in its name.</small></div>
  <ul class="nets" id="nets"></ul>
  <form id="join" autocomplete="off">
    <label for="ssid">Network name</label><input id="ssid" type="text" maxlength="32" required>
    <label for="psk">Password</label><input id="psk" type="password" maxlength="63" placeholder="Leave empty for an open network">
    <label class="check"><input type="checkbox" id="hidden"> Hidden network</label>
    <div class="row" style="margin-top:14px">
      <button type="submit" id="connect">Connect now</button>
      <button type="button" class="ghost" id="save">Save for later</button>
    </div>
    <p><small>Connecting now moves the Pi to that network. If you're using this page over the Pi's current Wi-Fi,
    you'll lose this page: join the same network on your phone and open <span class="mono">http://{{HOST}}.local</span> again.</small></p>
  </form>
  <div id="join-msg"></div>
</section>

<section class="card" aria-labelledby="h-saved">
  <h2 id="h-saved">Saved networks</h2>
  <table id="saved"></table>
</section>

<script>
const H = {"Content-Type":"application/json","X-Star-Admin":"1"};
const $ = id => document.getElementById(id);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
async function post(path, body) {
  const r = await fetch(path, {method:"POST", headers:H, body:JSON.stringify(body||{}), credentials:"same-origin"});
  if (r.status === 401) { location.reload(); throw new Error("signed out"); }
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || ("Error " + r.status));
  return j;
}
function bars(sig){ return sig>=75?"▂▄▆█":sig>=50?"▂▄▆ ":sig>=25?"▂▄  ":"▂   "; }
function render(s) {
  const wifi = s.wifi ? `${esc(s.wifi.ssid)} <span class="bars">${bars(s.wifi.signal)} ${s.wifi.signal}%</span>` : "Not connected";
  let rows = `<dt>Wi-Fi</dt><dd>${wifi}</dd><dt>Gateway</dt><dd class="mono">${esc(s.gateway)||"none"}</dd>`;
  for (const i of s.interfaces) {
    if (!i.addresses.length) continue;
    const a = i.addresses.map(x => `<span class="mono">${esc(x.ip)}</span><small> /${x.prefix}${x.dhcp?" · DHCP":" · fixed"}</small>`).join("<br>");
    rows += `<dt>${esc(i.name)}</dt><dd>${a}</dd>`;
  }
  $("net").innerHTML = rows;
  $("printers").innerHTML = s.printers.map(p => {
    const st = p.status || {};
    const bt = p.bluetooth === "connected" ? "ok" : (p.bluetooth === "connecting" ? "warn" : "bad");
    return `<div class="printer"><div class="row" style="justify-content:space-between"><strong>${esc(p.name)}</strong>
      <span class="pill ${bt}" title="Bluetooth link to the printer">${esc(p.bluetooth)}</span></div>
      <dl class="kv" style="margin-top:8px"><dt>Address</dt><dd class="mono">${esc(p.address) || "waiting for an address…"}</dd>
      <dt>Device</dt><dd class="mono">${esc(p.device)}</dd>
      <dt>CUPS</dt><dd class="mono">socket://${esc(p.local||"127.0.0.1")}:9100</dd>
      <dt>Status</dt><dd><span class="pill ${esc(st.state)}" title="${esc(st.raw)}">${esc(st.text)}</span></dd></dl></div>`;
  }).join("");
  $("saved").innerHTML = s.saved.length ? s.saved.map(n => `<tr><td><strong>${esc(n.name)}</strong>
      ${n.active?'<span class="pill ok">in use</span>':""}</td>
      <td style="text-align:right">${n.active?"":`<button class="ghost" data-forget="${esc(n.uuid)}" data-name="${esc(n.name)}">Forget</button>`}</td></tr>`).join("")
    : "<tr><td>No saved networks.</td></tr>";
  const j = s.join || {};
  if (joinMessageCurrent(j, s.wifi)) {
    const cls = {ok:"ok", saved:"ok", error:"bad", connecting:"warn"}[j.state] || "unknown";
    const extra = j.addresses ? `<br>Addresses: <span class="mono">${j.addresses.map(esc).join(", ")}</span>` : "";
    $("join-msg").innerHTML = `<div class="msg ${cls}">${esc(j.text)}${extra}</div>`;
  } else if (j.state) {
    $("join-msg").innerHTML = "";
  }
}
// Show the last join result only while it's still true: "connecting" until it
// finishes; "Connected to X" only while the Pi is still on X; "saved" and
// errors for 10 minutes. Otherwise it would keep saying "Connected to" a phone
// hotspot after the Pi has gone back to the venue network.
function joinMessageCurrent(j, wifi) {
  if (!j.state) return false;
  if (j.state === "connecting") return true;
  if (j.state === "ok") return !!(wifi && wifi.ssid === j.ssid);
  return Date.now() / 1000 - (j.time || 0) < 600;
}
async function refresh() {
  try {
    const r = await fetch("/api/status?t=" + Date.now(), {credentials:"same-origin", cache:"no-store"});
    if (r.status === 401) return location.reload();
    if (!r.ok) throw new Error("HTTP " + r.status);
    render(await r.json());
    $("updated").className = "sub";
    $("updated").textContent = "Updated " + new Date().toLocaleTimeString();
  } catch (e) {
    $("updated").className = "msg warn";
    $("updated").textContent = "Can't reach the Pi right now. The details below may be out of date.";
  }
}
// Refresh straight away when the page comes back into view: switching tabs,
// unlocking a phone, or the browser restoring it from its back/forward cache.
document.addEventListener("visibilitychange", () => { if (!document.hidden) refresh(); });
window.addEventListener("pageshow", e => { if (e.persisted) refresh(); });
$("scan").onclick = async () => {
  $("scan").disabled = true; $("scan-note").textContent = "Scanning… (about 10 seconds)";
  try {
    const {networks} = await post("/api/scan");
    $("nets").innerHTML = networks.map(n => `<li data-ssid="${esc(n.ssid)}"><span class="name">${esc(n.ssid)}</span>
      ${n.in_use?'<span class="pill ok">connected</span>':""}<small>${esc(n.security)}</small>
      <span class="bars">${bars(n.signal)} ${n.signal}%</span></li>`).join("") || "<li>No networks found.</li>";
    $("scan-note").textContent = "Tap a network to fill in its name.";
  } catch (e) { $("scan-note").textContent = e.message; }
  $("scan").disabled = false;
};
$("nets").onclick = e => { const li = e.target.closest("li[data-ssid]"); if (li) { $("ssid").value = li.dataset.ssid; $("psk").focus(); } };
async function join(now) {
  const body = {ssid:$("ssid").value, password:$("psk").value, hidden:$("hidden").checked, connect_now:now};
  $("connect").disabled = $("save").disabled = true;
  try {
    await post("/api/join", body);
    $("psk").value = "";
    $("join-msg").innerHTML = `<div class="msg warn">${now ? "Connecting to " + esc(body.ssid) + "…" : "Saved."}</div>`;
  } catch (e) { $("join-msg").innerHTML = `<div class="msg bad">${esc(e.message)}</div>`; }
  $("connect").disabled = $("save").disabled = false;
  setTimeout(refresh, 1500);
}
$("join").onsubmit = e => { e.preventDefault(); join(true); };
$("save").onclick = () => { if ($("join").reportValidity()) join(false); };
$("saved").onclick = async e => {
  const b = e.target.closest("button[data-forget]"); if (!b) return;
  b.textContent = "Tap again to forget"; if (!b.dataset.armed) { b.dataset.armed = 1; return; }
  try { await post("/api/forget", {uuid:b.dataset.forget}); }
  catch (err) { $("join-msg").innerHTML = `<div class="msg bad">${esc(err.message)}</div>`; }
  refresh();
};
$("logout").onclick = async () => { try { await post("/logout"); } catch (e) {} location.reload(); };
refresh(); setInterval(refresh, 5000);
</script>
"""


def main():
    ap = argparse.ArgumentParser(description="Web page for joining Wi-Fi and checking printer addresses.")
    ap.add_argument("--set-password", action="store_true", help="set the admin password and exit")
    ap.add_argument("--listen", default="0.0.0.0", help="address to listen on (default: all)")
    ap.add_argument("--port", type=int, default=80, help="port (default: 80)")
    a = ap.parse_args()
    if a.set_password:
        raise SystemExit(set_password_interactive())
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    threading.Thread(target=DHCP.run_forever, daemon=True, name="dhcp").start()
    srv = http.server.ThreadingHTTPServer((a.listen, a.port), Handler)
    srv.daemon_threads = True
    log.info("star-admin on http://%s:%d (%s)", a.listen, a.port,
             "password set" if load_auth() else "NO PASSWORD SET: page is locked")
    srv.serve_forever()


if __name__ == "__main__":
    main()
