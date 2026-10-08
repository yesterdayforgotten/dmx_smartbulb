#!/usr/bin/env python3
"""Network load shaped like the engine's bulb traffic: small Kasa-sized UDP
packets (XOR-obfuscated JSON, ~180 bytes) as if driving N bulbs at R Hz each,
with periodic bursts.

Packets go to the default gateway's discard port (UDP 9), never to bulbs (they
would change color) and never as broadcast (that would load the WiFi the bulbs
share).

    python3 tools/net_load.py                  # 30 bulbs x 20 Hz = 600 pkt/s, bursts of 3000 pkt/s
    python3 tools/net_load.py --bulbs 60 --hz 30 --duration 600
"""

import argparse
import json
import socket
import subprocess
import time


def kasa_encrypt(data):
    key = 171
    out = bytearray()
    for b in data:
        key ^= b
        out.append(key)
    return bytes(out)


def gateway():
    out = subprocess.run(["ip", "route"], capture_output=True, text=True).stdout
    for line in out.splitlines():
        if line.startswith("default"):
            return line.split()[2]
    raise SystemExit("no default gateway")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bulbs", type=int, default=30)
    ap.add_argument("--hz", type=float, default=20)
    ap.add_argument("--burst-rate", type=float, default=3000, help="packets/s during bursts")
    ap.add_argument("--burst-every", type=float, default=10, help="seconds between 1 s bursts")
    ap.add_argument("--duration", type=float, default=0)
    args = ap.parse_args()

    target = (gateway(), 9)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setblocking(False)
    payloads = [kasa_encrypt(json.dumps(
        {"smartlife.iot.smartbulb.lightingservice": {"transition_light_state": {
            "ignore_default": 1, "on_off": 1, "transition_period": 30,
            "hue": (i * 37) % 360, "saturation": 100, "color_temp": 0, "brightness": 80}}},
        separators=(",", ":")).encode()) for i in range(args.bulbs)]

    start = time.monotonic()
    sent = dropped = 0
    next_t = start
    print(f"sending to {target[0]}:9, {len(payloads[0])}-byte packets", flush=True)
    while not args.duration or time.monotonic() - start < args.duration:
        now = time.monotonic()
        in_burst = (now - start) % args.burst_every < 1.0
        rate = args.burst_rate if in_burst else args.bulbs * args.hz
        try:
            sock.sendto(payloads[sent % len(payloads)], target)
            sent += 1
        except BlockingIOError:
            dropped += 1
        next_t += 1 / rate
        delay = next_t - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        elif delay < -0.5:
            next_t = time.monotonic()  # fell behind; don't try to catch up
    print(f"sent {sent} packets, {dropped} dropped by the socket")


if __name__ == "__main__":
    main()
