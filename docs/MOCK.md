# How the mock printer works

[`tools/mock_printer.py`](../tools/mock_printer.py) stands in for a Bluetooth
TSP100III so the bridge can be tested without printers. For how to run it on
a test Pi, see [TESTING.md](TESTING.md).

The bridge never talks to the printer directly. It opens a device file,
`/dev/rfcomm0`, reads and writes bytes, and the kernel carries those bytes over
Bluetooth. The mock printer gives the bridge a different file that behaves the
same way, and answers from Python instead of from a printer.

## 1. A fake serial cable: the pseudo-terminal

`pty.openpty()` asks Linux for a **pseudo-terminal**, a pair of connected ends
like a virtual serial cable.

- The **slave** end looks like a serial port, for example `/dev/pts/2`. The
  bridge opens this end, just as it would open `/dev/rfcomm1`.
- The **master** end stays inside the mock. Whatever the bridge writes to the
  slave end, the mock reads from the master end, and the other way round.
- `tty.setraw()` turns off line handling, so bytes like `0x1B` pass through
  untouched, as they would over Bluetooth.
- A symlink such as `~/mock/TSP143-2 → /dev/pts/2` gives it a stable name for
  the config's `device =` line.

The bridge checks the Bluetooth link state only for `/dev/rfcommN` devices.
Anything else, the mock's pty included, counts as always connected.

## 2. Waiting for data

`run()` loops forever. `select()` waits up to 50 ms for bytes from the bridge.
Anything that arrives goes to `handle()`, and every pass also calls `tick()`
for things that depend on time.

## 3. Answering commands (`handle`)

It looks for the few commands the bridge relies on in the incoming bytes:

| Bytes | Meaning | Mock's response |
|---|---|---|
| `1B 06 01` | "send me your status" | one status frame |
| `1B 1E 61 n` | "turn on automatic status" | one status frame |
| `1B 1E 45 n` | "reset the job counter" | counter to 0, then a status frame |
| `17` (ETB) | "end of job" | counter + 1, then a status frame |

It keeps the last 4 bytes of each read (`self.tail`) and checks them together
with the next read. That way a command split across two reads is still
recognised.

## 4. The status frame (`frame`)

A real TSP100III over Bluetooth replies with 9 bytes, starting `23 06`. The
mock builds the same thing:

- **Byte 2** gets bit `0x08` (offline) when the cover is open or the paper is
  out, plus `0x20` for cover open.
- **Byte 5** gets bit `0x08` when the paper is out.
- **Byte 7** carries the ETB counter, shifted left one bit, exactly where Star
  puts it.

The bridge converts this to the 11-byte LAN version (`23 86 …`), so the POS
can't tell it's not real. `kill -USR1` and `kill -USR2` flip the paper-out and
cover-open flags, so you can test how the POS handles a printer problem.

## 5. The one bit of judgement: is `0x17` really the end of the job?

Print data is compressed image data, and `0x17` can turn up inside it by
chance. A real printer parses every command, so it knows the difference. The
mock uses a shortcut instead: a `0x17` counts only if it's the **last byte of a
read** and **nothing else arrives within 0.1 s** (`ETB_SETTLE`). A POS sends
ETB last and then waits for the counter to rise, so that matches real traffic.
If more data does follow straight away, `handle()` cancels it as raster data.

## 6. Saving jobs (`finish_job`)

Every byte received goes into `self.job`. After 1.5 s of silence (`JOB_GAP`),
the collected bytes are written to `job-<date>-<time>-<n>.bin` in the jobs
directory. Anything of 16 bytes or less is dropped as status chatter, not a
job. A saved job can be a few bytes bigger than what the POS sent: the bridge's
own 3-byte status request (`1B 06 01`) is saved along with the receipt if it
arrives during the job.

## What it doesn't do

- It doesn't decode or render the receipt; the `.bin` file is the raw bytes.
- It doesn't simulate Bluetooth dropping out or the printer being switched off.
- A command that lands in the last 4 bytes of a read can be matched again on
  the next read and answered with an extra status frame. That's harmless for
  testing, since the bridge just keeps the newest status.
