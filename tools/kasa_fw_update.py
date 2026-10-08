#!/usr/bin/env python3
"""Update a Kasa bulb's firmware over the local protocol, one step at a time.

Uses the firmware URL the bulb itself reports from TP-Link's cloud (the same
image the Kasa app would install). Run the steps in order and check each one:

    python3 tools/kasa_fw_update.py IP list       # current version + offered update (read-only)
    python3 tools/kasa_fw_update.py IP download   # bulb downloads the image; shows progress
    python3 tools/kasa_fw_update.py IP download http://PI/fw.bin   # ...from a URL we serve
    python3 tools/kasa_fw_update.py IP status     # download state (read-only)
    python3 tools/kasa_fw_update.py IP flash      # flash it (irreversible); bulb reboots

Don't power-cycle the bulb while it downloads or flashes. The flash step
refuses to run unless the download state says it's complete.

On a KL135 (1.0.10 -> 1.0.15, 2026-10-07) the bulb flashed and rebooted by
itself as soon as the download finished (status 2 = downloaded, flashing), so
the flash step wasn't needed; run `list` afterwards to confirm the version.
"""

import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from engine import kasa  # noqa: E402

SYS = "smartlife.iot.common.system"
CLOUD = "smartlife.iot.common.cloud"


async def call(t, ip, ns, method, params=None, timeout=5):
    r = await t.request(ip, {ns: {method: params or {}}}, timeout=timeout)
    return None if r is None else r.get(ns, {}).get(method)


async def offered(t, ip):
    fw = await call(t, ip, CLOUD, "get_intl_fw_list", timeout=10)
    lst = (fw or {}).get("fw_list") or []
    return lst[0] if lst else None


async def main():
    if len(sys.argv) not in (3, 4) or sys.argv[2] not in ("list", "download", "status", "flash"):
        sys.exit(__doc__)
    ip, step = sys.argv[1], sys.argv[2]
    url = sys.argv[3] if len(sys.argv) == 4 else None
    t = await kasa.KasaTransport.create()
    try:
        info = await t.request(ip, kasa.SYSINFO, timeout=3)
        if not info:
            sys.exit(f"{ip} doesn't answer")
        si = info["system"]["get_sysinfo"]
        print(f"{ip}: {si.get('model')} fw {si.get('sw_ver')} mac {kasa.sysinfo_mac(si)}")

        if step == "list":
            fw = await offered(t, ip)
            print("offered:", json.dumps(fw, indent=1) if fw else "nothing")

        elif step == "status":
            print("download state:", json.dumps(await call(t, ip, SYS, "get_download_state")))

        elif step == "download":
            if url is None:
                fw = await offered(t, ip)
                if not fw:
                    sys.exit("the bulb isn't offered an update")
                url = fw["fwUrl"]
                print(f"asking the bulb to download {fw['fwVer']}")
            print(f"  from {url}")
            print("download_firmware ->", json.dumps(await call(t, ip, SYS, "download_firmware", {"url": url})))
            start, last = time.monotonic(), None
            while time.monotonic() - start < 300:
                s = await call(t, ip, SYS, "get_download_state", timeout=3)
                if s != last:
                    print(f"{time.monotonic() - start:6.1f}s {json.dumps(s)}", flush=True)
                    last = s
                if s and (s.get("err_code", 0) != 0 or s.get("ratio") == 100):
                    break
                if s and s.get("status") == 0 and time.monotonic() - start > 30:
                    print("the download never started (status stayed 0)")
                    break
                await asyncio.sleep(2)

        elif step == "flash":
            s = await call(t, ip, SYS, "get_download_state", timeout=3)
            print("download state:", json.dumps(s))
            if not s or s.get("ratio") != 100 or s.get("err_code", 0) != 0:
                sys.exit("not flashing: the download isn't reported complete (ratio 100)")
            print("flash_firmware ->", json.dumps(await call(t, ip, SYS, "flash_firmware")))
            print("flashing; the bulb reboots. Waiting for it to come back...")
            await asyncio.sleep(15)
            for _ in range(36):
                found = await kasa.discover(["255.255.255.255"], timeout=2)
                for fip, finfo in found.items():
                    if kasa.sysinfo_mac(finfo) == kasa.sysinfo_mac(si):
                        print(f"back at {fip}: fw {finfo.get('sw_ver')}")
                        return
                await asyncio.sleep(3)
            print("not seen again yet; check with: python3 tools/kasa_fw_update.py IP list (it may have a new IP)")
    finally:
        t.close()


if __name__ == "__main__":
    asyncio.run(main())
