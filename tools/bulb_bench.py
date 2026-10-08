#!/usr/bin/env python3
"""Measure how fast real Kasa bulbs accept commands, safely.

Single bulb: steps the command rate up (2, 5, 8 ... 50 Hz by default), each step
for --step seconds, measuring reply rate and round-trip time. Every command
carries a unique hue/brightness pair, and the bulb echoes its new state in the
reply, so each reply is matched to its command exactly.

It stops at the first sign of trouble: replies below --min-reply, RTT p95 above
--rtt-factor x the first step's p95, or a failed health check between steps.
After stopping it rests, checks the bulb still answers, and puts the bulb back
to the state it had before the run.

    python3 tools/bulb_bench.py 192.168.10.178
    python3 tools/bulb_bench.py 192.168.10.178 192.168.10.158 --rates 5,10,15,20
    python3 tools/bulb_bench.py --all 192.168.10.178 192.168.10.158 --rate 10

--all runs every listed bulb at once at --rate each (for the rig-wide budget).
Results are printed and saved as JSON next to --out.
"""

import argparse
import asyncio
import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from engine import kasa  # noqa: E402

LIGHT = kasa.LIGHTING
DEFAULT_RATES = (2, 5, 8, 10, 12, 15, 20, 25, 30, 40, 50)
SAT, BRI0 = 100, 40


def tag_state(i):
    """A unique (hue, brightness) for command i, repeating every 720 commands."""
    t = i % 720
    return t % 360, BRI0 + t // 360


class Probe:
    """Sends tagged commands to one bulb and matches the replies."""

    def __init__(self, ip):
        self.ip = ip
        self.sent = {}      # tag -> send time
        self.rtts = []
        self.replies = 0
        self.unmatched = 0
        self.counter = 0

    def command(self, transition_ms):
        h, b = tag_state(self.counter)
        self.counter += 1
        return (h, b), kasa.light_state(h, SAT, b, transition_ms)

    def on_reply(self, reply, t):
        st = reply.get(LIGHT, {}).get("transition_light_state")
        if not st:
            return
        key = (st.get("hue"), st.get("brightness"))
        sent = self.sent.pop(key, None)
        if sent is None:
            self.unmatched += 1
            return
        self.replies += 1
        self.rtts.append(t - sent)

    def reset(self):
        self.sent.clear()
        self.rtts.clear()
        self.replies = self.unmatched = 0


def pct(values, p):
    if not values:
        return None
    v = sorted(values)
    return v[min(len(v) - 1, int(len(v) * p))]


async def run_step(transport, probes, rate, seconds):
    """Send to every probe at `rate` Hz for `seconds`, then wait for stragglers."""
    for p in probes:
        p.reset()
    interval = 1 / rate
    transition = int(interval * 1000)
    start = time.monotonic()
    n = 0
    while True:
        target = start + n * interval
        now = time.monotonic()
        if target - start >= seconds:
            break
        if target > now:
            await asyncio.sleep(target - now)
        for p in probes:
            key, cmd = p.command(transition)
            p.sent[key] = time.monotonic()
            transport.send(p.ip, cmd)
        n += 1
    await asyncio.sleep(1.0)    # let late replies arrive
    out = []
    for p in probes:
        rtt = [x * 1000 for x in p.rtts]
        out.append({
            "ip": p.ip, "rate_hz": rate, "sent": n, "replies": p.replies,
            "reply_rate": round(p.replies / n, 4) if n else None,
            "rtt_p50_ms": round(pct(rtt, 0.5), 1) if rtt else None,
            "rtt_p95_ms": round(pct(rtt, 0.95), 1) if rtt else None,
            "rtt_max_ms": round(max(rtt), 1) if rtt else None,
            "unmatched": p.unmatched,
        })
    return out


async def health(transport, ip, timeout=1.0, tries=3):
    for _ in range(tries):
        r = await transport.request(ip, kasa.SYSINFO, timeout)
        if r:
            return r["system"]["get_sysinfo"]
    return None


def restore_command(light):
    """A command that puts the bulb back to the light_state it reported."""
    if not light.get("on_off", 1):
        dft = light.get("dft_on_state", {})
        return {LIGHT: {"transition_light_state": {"on_off": 0, "transition_period": 0,
                                                   **{k: dft[k] for k in ("hue", "saturation", "brightness", "color_temp") if k in dft}}}}
    st = {k: light[k] for k in ("hue", "saturation", "brightness", "color_temp") if k in light}
    return {LIGHT: {"transition_light_state": {"on_off": 1, "ignore_default": 1, "transition_period": 0, **st}}}


async def bench_single(transport, ip, rates, step_s, rest_s, min_reply, rtt_factor, by_ip):
    print(f"\n=== {ip} ===", flush=True)
    info = await health(transport, ip)
    if not info:
        print("  doesn't answer get_sysinfo; skipping")
        return {"ip": ip, "error": "no answer"}
    original = info.get("light_state", {})
    print(f"  {info.get('model')} fw {info.get('sw_ver')} mac {kasa.sysinfo_mac(info)} rssi {info.get('rssi')}")
    probe = Probe(ip)
    by_ip[ip] = probe
    steps, baseline_p95, stop_reason = [], None, None
    try:
        for rate in rates:
            res = (await run_step(transport, [probe], rate, step_s))[0]
            steps.append(res)
            print(f"  {rate:5.1f} Hz: replies {res['replies']:4d}/{res['sent']:<4d} ({res['reply_rate'] * 100:5.1f}%)  "
                  f"rtt p50 {res['rtt_p50_ms']} p95 {res['rtt_p95_ms']} max {res['rtt_max_ms']} ms", flush=True)
            if baseline_p95 is None and res["rtt_p95_ms"]:
                baseline_p95 = res["rtt_p95_ms"]
            if res["reply_rate"] is None or res["reply_rate"] < min_reply:
                stop_reason = f"replies fell to {res['reply_rate']:.1%} at {rate} Hz"
                break
            if baseline_p95 and res["rtt_p95_ms"] and res["rtt_p95_ms"] > max(rtt_factor * baseline_p95, baseline_p95 + 30):
                stop_reason = f"RTT p95 rose to {res['rtt_p95_ms']} ms at {rate} Hz (baseline {baseline_p95} ms)"
                break
            await asyncio.sleep(rest_s)
            if not await health(transport, ip):
                stop_reason = f"no answer to the health check after {rate} Hz"
                break
    finally:
        await asyncio.sleep(rest_s)
        alive = await health(transport, ip, timeout=1.0, tries=5)
        waited = 0
        while not alive and waited < 60:
            print("  bulb isn't answering; waiting for it to recover...", flush=True)
            await asyncio.sleep(5)
            waited += 5
            alive = await health(transport, ip, timeout=1.0, tries=2)
        if alive:
            transport.send(ip, restore_command(original))
            print("  restored the bulb's previous state")
        else:
            print("  THE BULB DIDN'T RECOVER: power-cycle it (off for 10 s, then on)")
    if stop_reason:
        print(f"  stopped: {stop_reason}")
    good = [s for s in steps if s["reply_rate"] and s["reply_rate"] >= min_reply
            and (not baseline_p95 or (s["rtt_p95_ms"] or 0) <= max(rtt_factor * baseline_p95, baseline_p95 + 30))]
    best = max((s["rate_hz"] for s in good), default=None)
    return {"ip": ip, "model": info.get("model"), "fw": info.get("sw_ver"), "mac": kasa.sysinfo_mac(info),
            "steps": steps, "stop_reason": stop_reason, "max_clean_hz": best,
            "recovered": bool(alive), "tested_up_to_hz": steps[-1]["rate_hz"] if steps else None}


async def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("ips", nargs="+")
    ap.add_argument("--rates", default=",".join(str(r) for r in DEFAULT_RATES))
    ap.add_argument("--step", type=float, default=10.0, help="seconds per rate step")
    ap.add_argument("--rest", type=float, default=2.0, help="seconds of rest between steps")
    ap.add_argument("--min-reply", type=float, default=0.95)
    ap.add_argument("--rtt-factor", type=float, default=3.0)
    ap.add_argument("--all", action="store_true", help="drive all listed bulbs together at --rate")
    ap.add_argument("--rate", type=float, default=10.0)
    ap.add_argument("--port", type=int, default=kasa.KASA_PORT)
    ap.add_argument("--out", default=f"bulb_bench-{time.strftime('%Y%m%d-%H%M%S')}.json")
    args = ap.parse_args()

    by_ip = {}

    def on_reply(ip, reply, t):
        p = by_ip.get(ip)
        if p:
            p.on_reply(reply, t)

    transport = await kasa.KasaTransport.create(on_reply, port=args.port)
    results = []
    try:
        if args.all:
            originals = {}
            for ip in args.ips:
                info = await health(transport, ip)
                if info:
                    originals[ip] = info.get("light_state", {})
                    by_ip[ip] = Probe(ip)
            print(f"driving {len(by_ip)} bulbs together at {args.rate} Hz each "
                  f"({len(by_ip) * args.rate:.0f} packets/s) for {args.step} s")
            res = await run_step(transport, list(by_ip.values()), args.rate, args.step)
            for r in res:
                print(f"  {r['ip']:15s} replies {r['reply_rate'] * 100:5.1f}%  rtt p50 {r['rtt_p50_ms']} p95 {r['rtt_p95_ms']} ms")
            await asyncio.sleep(args.rest)
            for ip, light in originals.items():
                transport.send(ip, restore_command(light))
            results = res
        else:
            rates = [float(r) for r in args.rates.split(",")]
            for ip in args.ips:
                results.append(await bench_single(transport, ip, rates, args.step, args.rest,
                                                  args.min_reply, args.rtt_factor, by_ip))
    finally:
        transport.close()
    Path(args.out).write_text(json.dumps(results, indent=1))
    print(f"\nsaved {args.out}")
    if not args.all:
        for r in results:
            if r.get("max_clean_hz"):
                print(f"{r['ip']}: clean up to {r['max_clean_hz']} Hz"
                      + (f" (stopped: {r['stop_reason']})" if r.get("stop_reason") else " (never hit a limit)"))


if __name__ == "__main__":
    asyncio.run(main())
