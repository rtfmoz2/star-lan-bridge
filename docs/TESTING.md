# Testing on a Pi without printers

`tools/mock_printer.py` stands in for a Bluetooth TSP100III. It creates a
pseudo-terminal that answers status requests and counts ETBs like the printer,
and saves each print job to a file instead of printing it. With two of them, a
spare Pi can run the whole two-printer setup (discovery, status, printing and
printer addresses) with no Bluetooth or paper. [MOCK.md](MOCK.md) explains how
the mock works.

`python3 -m unittest discover tests` covers the same logic on any machine. This
page is for checking it on a real network with a real router.

## 1. Set up the Pi

Flash Raspberry Pi OS Lite (64-bit) as in [SETUP.md §1](SETUP.md#1-flash-and-reach-the-pi),
join the network you want to test on, and SSH in. Then:

```bash
# Pi
sudo apt install -y git busybox
git clone https://github.com/rtfmoz2/star-lan-bridge.git
cd star-lan-bridge
git switch <branch to test>      # skip for main
sudo ./scripts/install-admin.sh
sudo python3 /opt/star-lan-bridge/star_admin.py --set-password
```

Only the admin service is installed. The bridge runs by hand in step 3, so no
RFCOMM bindings are needed.

## 2. Start two mock printers

In a terminal on the Pi (leave it running):

```bash
# Pi
python3 tools/mock_printer.py --link ~/mock/TSP143 & python3 tools/mock_printer.py --link ~/mock/TSP143-2
```

Each logs `mock printer on /dev/pts/N, linked at …`, and later one line per
job received. Jobs are saved in `~/mock/TSP143-jobs/` and `~/mock/TSP143-2-jobs/`.
To test the POS's paper-out handling, `pkill -USR1 -f 'mock_printer.py --link .*TSP143-2'`
toggles "out of paper" on printer 2 (`-USR2` toggles "cover open").

## 3. Configure and run the bridge

```bash
# Pi
sudo tee /etc/star-lan-bridge.conf >/dev/null <<EOF
[printer:TSP143]
device  = $HOME/mock/TSP143
address = auto
local   = 127.0.0.1

[printer:TSP143-2]
device  = $HOME/mock/TSP143-2
address = next
local   = 127.0.0.2
mac     = 00:11:62:00:00:02
EOF
sudo systemctl restart star-admin
sleep 10; cat /run/star-lan-bridge/lease-TSP143-2       # ip=<.200, or the next free one after it>
python3 star_lan_bridge.py -c /etc/star-lan-bridge.conf
```

The bridge logs one `listening on …` line per printer and `printer: connected
via …/mock/…`. It restarts by itself when an address changes; when running it
by hand like this, start it again after it exits.

## 4. Checks

Run these from another machine on the same network (a Mac with the repository).

| Check | How | Expect |
|---|---|---|
| Next address | admin page, **Printers** | TSP143 on the Pi's address, TSP143-2 on the first free one from .200 |
| Discovery | `python3 tools/star_probe.py discover --broadcast <broadcast-address>` | two replies, one from each address |
| Status | `python3 tools/star_probe.py status --host <printer-2-address>` | `22 bytes`, `etb_counter=0` |
| Print path | `python3 tools/star_probe.py etb --host <printer-2-address>` | counter goes 0 → 1 |
| POS | search for Star LAN printers, print a test receipt to each | two printers found; a job file appears for each, no duplicates |
| Address in use | before step 3, put a device on `.200` (below) | TSP143-2 skips it and takes `.201` |
| Device turns up later | add the address to a device after TSP143-2 has it | within 2 minutes the admin page warns under TSP143-2 and star-admin logs `next: another device is also using …`; the printer **doesn't move** |
| Find another address | press the button under TSP143-2 twice | TSP143-2 moves to the next free address within a few seconds; the warning goes |
| Same address after reboot | `sudo reboot`, then repeat step 2 and the bridge | TSP143-2 gets the same address as before |
| Clash doesn't stop the bridge | give both printers the same fixed `address = <ip>` | the bridge serves TSP143, logs `address … is already used by printer TSP143`, keeps running |

To make an address look taken from a Mac (for one test, removed on reboot):

```bash
# Mac: pretend to be <address>; replace en0 with your Wi-Fi interface if different
sudo ifconfig en0 alias <address> 255.255.255.255
```

```bash
# Mac: stop pretending
sudo ifconfig en0 -alias <address>
```

### A router that ignores DHCP client IDs

To check the `dhcp` fallback, set `address = dhcp` for TSP143-2 and join a
network whose router leases by MAC only (some 4G and small business routers; a
phone hotspot may behave this way too). The admin page should show the note
*This router gives every printer the Pi's own address…* under TSP143-2 with
the first free address from `.200`, and `journalctl -t star-dhcp` should show
`refusing … router ignores the client ID`. Routers that honour client IDs,
UniFi included, give TSP143-2 its own lease instead, which is also correct.

## 5. Clean up

```bash
# Pi
sudo systemctl disable --now star-admin
sudo rm /etc/star-lan-bridge.conf
```
