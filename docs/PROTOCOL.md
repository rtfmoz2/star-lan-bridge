# Star LAN printer protocols, as used by star-lan-bridge

This document describes what a StarIO-based client (Star's SDKs and the POS
apps built on them) expects from a Star LAN receipt printer, and how
star-lan-bridge provides it in front of a Bluetooth printer.

**How much of this is certain.** Star doesn't publish a complete byte-level
specification for discovery or the 9101 channel. Each fact below is tagged
with its source:

| Tag | Meaning |
|---|---|
| **[observed]** | Seen directly in packet captures and bridge logs while building this project (Next Order POS 6.0.0 on a SunMi D3 Pro, a Bluetooth TSP143IIIBI, macOS CUPS). |
| **[reference]** | From the reverse-engineering notes in [gnaunited/tsp143-bridge](https://github.com/gnaunited/tsp143-bridge) (captured from a real TSP100IIILAN, firmware V2.2). |
| **[Star]** | From Star Micronics' published command manuals (StarPRNT / Star Line Mode command specifications, ASB specification). |
| **[unconfirmed]** | Seen but not decoded. Don't rely on it. |

Offsets are zero-based hex. Multi-byte integers are big-endian unless stated.

---

## 1. Overview

A Star LAN printer exposes three endpoints. All three are fixed by the
protocol; clients don't let you change them.

| Endpoint | Transport | Purpose |
|---|---|---|
| UDP 22222 | broadcast or unicast | **Star Discovery Protocol (SDP)**: "which Star printers are here?" |
| TCP 9100 | stream, bidirectional | **Print channel**: Star commands in; ASB status frames out |
| TCP 9101 | short-lived stream | **Status channel**: polled during a job to read the ETB counter |

A typical job from a StarIO client **[observed]**:

```
client                                         printer (bridge)
  |-- UDP 22222  STR_BCAST query (x2) ------------->|
  |<------------------- 302-byte identity reply ----|
  |-- TCP 9100 connect ---------------------------->|
  |<-------------------- ASB frame (11 B) greeting -|
  |-- ESC RS a 0 (ASB off) ------------------------>|   <- stripped by the bridge
  |-- ESC * r B, ESC GS ETX ..., ESC RS E 0, ------>|
  |   ESC ACK SOH, raster data ..., ETB             |
  |                                                 |
  |-- TCP 9101 connect, 51-byte query ------------->|   repeated ~5x per second
  |<--------------- 2 x 11-byte ASB frames, close --|   until the ETB counter rises
  |                                                 |
  |-- TCP 9100 close ------------------------------>|
```

The client decides the job succeeded when the **ETB counter** it reads back
(on 9100 or 9101) reaches the value it expects. If it never does, the client
waits about **30 seconds**, gives up, rediscovers the printer and **sends the
whole job again**, which produces duplicate receipts **[observed]**.

---

## 2. Star Discovery Protocol (UDP 22222)

### 2.1 Query (28 bytes)

| Offset | Size | Value | Meaning |
|---|---|---|---|
| 0x00 | 16 | `"STR_BCAST"` + 7 × `00` | Magic, NUL-padded to a 16-byte cell **[reference][observed]** |
| 0x10 | 8 | `"RQ1.0.0"` + `00` | Request version **[reference][observed]** |
| 0x18 | 2 | `00 1C` | Total length = 28 **[reference][observed]** |
| 0x1A | 2 | e.g. `64 31` | Request id; arbitrary, not echoed in the reply **[reference]** |

Hex of a complete query:

```
53 54 52 5f 42 43 41 53 54 00 00 00 00 00 00 00   STR_BCAST.......
52 51 31 2e 30 2e 30 00 00 1c 64 31               RQ1.0.0...d1
```

Sending rules:

* To **search**, clients broadcast to the subnet (`192.168.1.255`) and/or
  `255.255.255.255`. Next Order POS sends a query about every 0.3 s for about
  10 s **[observed]**.
* To **check** a printer it already knows, the client unicasts the query to
  the printer's IP. Next Order POS uses this as its "connected" test, and
  makes no TCP connection between jobs **[observed]**.
* A real printer answers only the exact structured query. A packet with just
  the magic gets no reply **[reference]**. star-lan-bridge is lenient and
  answers anything containing `STR_BCAST`.
* Discovery is link-local in practice: routers don't forward UDP 22222, so the
  bridge and the client must share a broadcast domain **[reference]**.

### 2.2 Reply (302 bytes)

Sent **unicast** back to the query's source address and port, from port 22222
**[observed]**.

| Offset | Size | Field | Value in the template | Notes |
|---|---|---|---|---|
| 0x000 | 16 | Magic | `"STR_BCAST"` + NULs | |
| 0x010 | 8 | Reply version | `"RS1.0.1\0"` | |
| 0x018 | 2 | Total length | `01 2E` (302) | |
| 0x01A | 8 | Offset table | `0022 0074 008A 012C` | Look like section offsets **[unconfirmed]** |
| 0x022 | 18 | Short model | `00` `"RTSP100LAN"` NULs | |
| 0x036 | 4 | Firmware version | `"V2.2"` | |
| 0x03E | 4 | Boot version | `"V2.2"` | |
| 0x044 | 1 | Flags | `20` | **[unconfirmed]** |
| 0x04C | 1 | Flags | `31` (`'1'`) | **[unconfirmed]** |
| 0x04E | 6 | **MAC address** | patched | Bridge: the interface's MAC |
| 0x058 | 4 | **IPv4 address** | patched | Bridge: the interface's IP |
| 0x05C | 4 | Addressing mode | `"DHCP"` | `"STAT"` for static **[reference]** |
| 0x06C | 4 | **Subnet mask** | patched | |
| 0x070 | 4 | **Default gateway** | patched | |
| 0x074 | 3 | Flags | `00 16 31` | **[unconfirmed]** |
| 0x084 | 5 | Flags | `30 00 31 00 31` (`'0' '1' '1'`) | **[unconfirmed]** |
| 0x088 | 4 | Counter / state | `31 00 00 A2` | Varies between captures on real hardware **[reference]**; sent as-is |
| 0x08C | 4 | Manufacturer | `"Star"` | |
| 0x0AC | 4 | Command set | `"STAR"` | |
| 0x0CC | 64 | Full model | `"TSP143IIILAN (STR_T-001)"` | What apps display |
| 0x10C | 7 | Device class | `"PRINTER"` | |
| 0x12C | 2 | Trailer | `00 02` | |

All other bytes are zero. star-lan-bridge sends the captured template verbatim
and patches only the four bold fields, choosing the interface on the client's
subnet when several are up.

### 2.3 Identity choices

* **Model.** The bridge always reports a TSP143IIILAN. Next Order POS accepted
  it with the printer configured in the app as a "TSP100IIIW" **[observed]**,
  so the app doesn't seem to require an exact model match.
* **MAC.** The bridge reports the Pi's own MAC (`dc:a6:32:…`), not a Star
  `00:11:62:…` address. No client rejected this **[observed]**. A client that
  pins printers by MAC would need a stable value, and the Pi's MAC is stable.
  With several printers, give each a distinct MAC (`mac =` in the config). The
  second printer in the reference setup reports its own Bluetooth MAC.

### 2.4 Several printers on one host

Clients key printers by IP address and the ports are fixed, so each printer
gets its own IPv4 address on the host (secondary addresses on one interface
work, including on Wi-Fi). The bridge then:

* answers a **broadcast** query with **one reply per printer**, each sent from
  that printer's own address so the reply's source matches the IP inside it;
* answers a **unicast** query only for the printer that owns the destination
  address. Linux delivers it to the socket bound to that exact address rather
  than the `0.0.0.0` socket.

Verified with two TSP143IIIBI printers at 192.168.1.134 and 192.168.1.250: a
broadcast got two replies (one per printer, each from its own address), and a
unicast got only its own printer **[observed]**.

---

## 3. ASB: Automatic Status Back

ASB frames are how the printer reports its state. They flow printer → host on
the print channel (USB, Bluetooth SPP or TCP 9100) **[Star]**.

### 3.1 Framing

Byte 0 encodes the frame length **[Star][reference]**:

| Bit | Meaning |
|---|---|
| 0 | Always 1. Marks the byte as an ASB header, not data |
| 1–3 | Low 3 bits of the frame length |
| 4 | Always 0 |
| 5–6 | High 2 bits of the frame length |
| 7 | Unused |

```python
length = ((b0 & 0b1110) >> 1) | ((b0 & 0b1100000) >> 2)
# 0x23 -> 9, 0x25 -> 10, 0x27 -> 11
```

### 3.2 USB/Bluetooth vs LAN shape

| Interface | Length | Byte 1 | Example (idle) |
|---|---|---|---|
| USB (TSP143IIIU) **[reference]** and Bluetooth SPP (TSP143IIIBI) **[observed]** | 9 | `06` | `23 06 00 00 00 00 00 00 00` |
| LAN (TSP100IIILAN) **[reference]** | 11 | `86` (bit 7 set) | `23 86 00 00 00 00 00 00 00 00 00` |

The byte 0 header stays `23` in both; the two extra LAN bytes aren't counted
in it **[reference]**. The bridge converts every frame from the printer by
setting bit 7 of byte 1 and padding with zeros to 11 bytes, so clients see the
LAN shape they expect from a network printer.

### 3.3 Fields the bridge depends on

| Byte | Bits | Meaning | Source |
|---|---|---|---|
| 2 | 1 | ETB command executed | **[reference]**; seen as `02` right after an ETB **[observed]** |
| 7 | 1–5 | ETB counter | **[reference][Star]**; `00` after `ESC RS E 0`, `02` (count 1) after one ETB **[observed]** |

The remaining bytes carry printer status, error and paper-sensor bits. See
Star's ASB specification for the full bit map. The bridge passes them through
unchanged and never interprets or synthesises them.

One more pattern was seen on a printer with **no paper loaded**: bit 3 of byte 2
and bit 3 of byte 5 set (`08`), in LAN shape
`23 86 08 00 00 08 00 02 00 00 00` **[observed]**. This is the status a POS app
reads to warn about paper; the job was still accepted on 9100 and logged as
finished.

Observed on the Bluetooth TSP143IIIBI over `/dev/rfcomm0`:

```
ESC ACK SOH            ->  23 06 00 00 00 00 00 00 00
ESC RS E 0, then ETB   ->  23 06 00 00 00 00 00 00 00     (counter cleared)
                           23 06 02 00 00 00 00 02 00     (ETB executed, counter = 1)
```

---

## 4. Print channel (TCP 9100)

Raw, bidirectional. The client writes Star commands; the printer writes ASB
frames back whenever its status changes (while ASB is on).

### 4.1 Commands that matter for status

| Bytes | Name | Effect | Source |
|---|---|---|---|
| `1B 1E 61 01` | ESC RS a 1 | Enable ASB | **[Star]** |
| `1B 1E 61 00` | ESC RS a 0 | Disable ASB | **[Star]** |
| `1B 1E 45 00` | ESC RS E 0 | Clear the ETB counter | **[Star][reference]** |
| `17` | ETB | Printer increments the ETB counter when it reaches this point | **[Star][reference]** |
| `1B 06 01` | ESC ACK SOH | Request one status frame | **[Star]**; answered with one ASB frame **[observed]** |
| `1B 2A 72 42` | ESC * r B | Quit raster mode | **[Star]** |
| `1B 1D 03 04 00 00` | ESC GS ETX … | Sent at job start by Next Order POS | **[unconfirmed]** |

### 4.2 A real job, start of stream **[observed]**

Next Order POS 6.0.0, one receipt, 48,437 bytes in 1,024-byte writes:

```
1b 1e 61 00             ESC RS a 0     ASB off
1b 2a 72 42             ESC * r B      leave raster mode
1b 1d 03 04 00 00       ESC GS ETX     (not decoded)
1b 1e 45 00             ESC RS E 0     clear ETB counter
1b 06 01                ESC ACK SOH    status request
...                     raster image data (TSP100 is raster-only)
17                      ETB            exactly one per job
```

### 4.3 Why ESC RS a 0 is stripped

The client switches ASB off, clears the counter, sends the job with one ETB,
and then polls TCP 9101 for the counter to rise. On a Bluetooth (or USB)
printer, the ETB counter only reaches the host as an ASB frame, and with ASB
off the printer never sends one. The client keeps reading `00`, times out after
30 s and reprints. Before the fix this produced four copies of one receipt in
two minutes **[observed]**.

star-lan-bridge therefore:

1. sends `ESC RS a 1` whenever it opens the printer link;
2. **removes the 4-byte sequence `1B 1E 61 00`** from client data, including
   when it is split across TCP reads (a trailing 1–3 byte prefix is held until
   the next read, or flushed after 100 ms of silence or at end of job);
3. sends `ESC RS a 1` again after each client disconnects.

After the fix the same job finished in 3 s with one copy and a final status of
`23 86 02 00 00 00 00 02 00 00 00` **[observed]**. The trade-off is that those
exact four bytes are also removed if they ever occur inside raster image data.
That's unlikely, and far less harmful than duplicate receipts.

### 4.4 Connection behaviour

* On connect, the bridge sends the cached status once (11 bytes) as a greeting.
  The client reads it before sending data **[observed]**.
* One client at a time. Others wait in the TCP backlog (8). CUPS uses this
  channel too, through `socket://127.0.0.1:9100`.
* The job is complete when the client closes its side. The bridge doesn't add
  any end-of-job handling.

---

## 5. Status channel (TCP 9101)

| Step | Direction | Bytes | Source |
|---|---|---|---|
| 1 | client → printer | TCP connect | **[reference][observed]** |
| 2 | client → printer | 51 bytes: `32` + 50 × `00` | **[reference]** |
| 3 | printer → client | 22 bytes: two identical 11-byte ASB frames (LAN shape) | **[reference][observed]** |
| 4 | printer | closes the connection (FIN) | **[reference][observed]** |

* During a job, Next Order POS polled 9101 about **5 times per second** (165
  polls in one 30-second attempt) **[observed]**.
* Between jobs that client made no 9101 connections at all **[observed]**.
* Before committing to a job, StarIO clients may make 2–3 quick
  connect/disconnect probes **[reference]**.
* The bridge answers from its cache of the printer's last real frame. If no
  print client owns the link and the cache is more than 1 s old, it first asks
  the printer with `ESC ACK SOH` (at most 0.6 s). Measured reply time: about
  5 ms cached, about 140 ms with a refresh **[observed]**.

---

## 6. Other traffic seen from POS clients

| Traffic | What it is | Bridge |
|---|---|---|
| UDP 3289 broadcast, 14 bytes `"EPSONQ"` + NULs, every ~10 s while searching | Epson ENPC printer discovery **[observed]** | Not answered |
| mDNS queries for `_ipp`, `_ipps`, `_printer`, `_pdl-datastream` | Generic OS printer browsing **[observed]** | Answered by Avahi/CUPS for the IPP queue, not by the bridge |

A real TSP100IIILAN doesn't answer mDNS, SNMP (UDP 161) or UDP 3289
**[reference]**, so the bridge doesn't either.

---

## 7. CUPS on the same printer

CUPS renders jobs with Star's `rastertostar` filter (the TSP100 series accepts
only raster graphics) and sends them with the `socket` backend to
`127.0.0.1:9100`. To the bridge, CUPS is just another print client: it
receives the status greeting and frames, which the socket backend ignores.

On Raspberry Pi OS, mask `colord`. Otherwise `gstoraster` waits for two 25-second
D-Bus timeouts per job, because `colord` exits as soon as it starts on a headless
system. That took jobs from 49 s down to 0.7 s **[observed]**.
