# Third-party notices

## gnaunited/tsp143-bridge

<https://github.com/gnaunited/tsp143-bridge>

star-lan-bridge doesn't include code from tsp143-bridge. It uses that project's
published protocol research: the 302-byte Star Discovery reply template captured
from a TSP100IIILAN (firmware V2.2), which appears in `DISCOVERY_TEMPLATE` in
`star_lan_bridge.py`, plus its documentation of ASB frame sizing, the
USB-to-LAN frame conversion and the TCP 9101 polling exchange. Its notice
follows:

```
MIT License

Copyright (c) 2026 Senternal LLC <https://senternaltech.com>

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## Star Micronics CUPS driver

`scripts/setup-cups.sh` builds Star's CUPS driver from the source bundled in
Star's own download. That driver isn't redistributed here and stays under
Star's license (see `SoftwareLicenseAgreement.pdf` in Star's archive). Download
it from Star Micronics.
