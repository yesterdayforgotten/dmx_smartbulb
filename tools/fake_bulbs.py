#!/usr/bin/env python3
"""Simulated Kasa KL135 bulbs for building and testing the engine without the rig.

Each bulb listens on its own loopback address (127.0.0.10, .11, ...) on UDP
9999, speaks the real XOR-framed protocol, and answers get_sysinfo, light-state
commands and power-on-default commands the way a KL135 does. Latency, packet
loss and offline bulbs can be set at start and changed while running.

    python3 tools/fake_bulbs.py --count 32 --latency 5 --loss 1
    # then type commands:  offline 3 | online 3 | latency 3 50 | loss 0 | stats | quit
    python3 tools/fake_bulbs.py --count 48 --mirror 192.168.10.1
    # --mirror HOST[:PORT] forwards a same-size copy of every received packet to
    # HOST (port 9, discard, by default), so the engine's traffic to the fake
    # bulbs also loads the real network. Point it at the dmx_esp32_wifisink
    # ESP32 (HOST:9999) to load the WiFi too; it answers each copy like a bulb.

The engine reaches them with kasa_port 9999 and bulb IPs 127.0.0.10+. Tests use
FakeBulbFleet directly.
"""

import argparse
import asyncio
import json
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from engine import kasa  # noqa: E402

LIGHTING = kasa.LIGHTING


MIRROR = {"sock": None, "addr": None, "count": 0}


class FakeBulb(asyncio.DatagramProtocol):
    def __init__(self, index, ip, rng, latency_ms=0.0, loss=0.0, port=kasa.KASA_PORT):
        self.index = index
        self.ip = ip
        self.port = port
        self.mac = "50C7BF%06X" % (0x100000 + index)
        self.alias = f"Fake bulb {index + 1}"
        self.latency_ms = latency_ms
        self.loss = loss
        self.online = True
        self.rng = rng
        self.state = {"on_off": 1, "mode": "normal", "hue": 0, "saturation": 0,
                      "color_temp": 2700, "brightness": 100}
        self.preferred = None
        self.received = 0          # every datagram that reached the bulb
        self.commands = 0          # light-state commands applied
        self.recent = []           # arrival times of light-state commands (for rate checks)
        self.transport = None

    def connection_made(self, transport):
        self.transport = transport

    def sysinfo(self):
        light = dict(self.state)
        if not light["on_off"]:
            light = {"on_off": 0, "dft_on_state": {k: v for k, v in self.state.items() if k != "on_off"}}
        return {
            "sw_ver": "1.0.6 Build 210330 Rel.173938", "hw_ver": "1.0", "model": "KL135(US)",
            "description": "Kasa Smart Light Bulb", "alias": self.alias, "mic_type": "IOT.SMARTBULB",
            "dev_state": "normal", "mic_mac": self.mac, "deviceId": "80" + self.mac * 3,
            "oemId": "FAKE", "hwId": "FAKE", "is_factory": False, "is_color": 1,
            "is_dimmable": 1, "is_variable_color_temp": 1, "light_state": light,
            "rssi": -50, "active_mode": "none", "err_code": 0,
        }

    def handle(self, cmd):
        if "system" in cmd and "get_sysinfo" in cmd["system"]:
            return {"system": {"get_sysinfo": self.sysinfo()}}
        if "system" in cmd and "set_dev_alias" in cmd["system"]:
            self.alias = cmd["system"]["set_dev_alias"]["alias"]
            return {"system": {"set_dev_alias": {"err_code": 0}}}
        svc = cmd.get(LIGHTING)
        if svc is None:
            return {"err_code": -1, "err_msg": "module not support"}
        out = {}
        if "get_light_state" in svc:
            st = dict(self.state)
            if not st["on_off"]:
                st = {"on_off": 0, "dft_on_state": {k: v for k, v in self.state.items() if k != "on_off"}}
            out["get_light_state"] = dict(st, err_code=0)
        if "transition_light_state" in svc:
            req = svc["transition_light_state"]
            self.commands += 1
            now = time.monotonic()
            self.recent.append(now)
            if len(self.recent) > 200:
                del self.recent[:100]
            for k in ("hue", "saturation", "brightness", "color_temp", "on_off"):
                if k in req:
                    self.state[k] = req[k]
            reply = dict(self.state)
            reply["err_code"] = 0
            out["transition_light_state"] = reply
        if "get_light_parameters" in svc:
            v = self.state["brightness"] if self.state["on_off"] else 0
            out["get_light_parameters"] = {"energy_usage_milliwatts": 300 + 66 * v, "brightness_lumens": 8 * v, "err_code": 0}
        if "get_default_behavior" in svc:
            pref = self.preferred or {"hue": 0, "saturation": 0, "color_temp": 2700, "brightness": 100}
            hard = ({"mode": "last_status"} if pref.get("last") else
                    {"mode": "customize_preset", "index": 0,
                     **{k: pref.get(k, 0) for k in ("hue", "saturation", "color_temp", "brightness")}})
            out["get_default_behavior"] = {"soft_on": {"mode": "last_status"}, "hard_on": hard, "err_code": 0}
        if "set_default_behavior" in svc:
            if svc["set_default_behavior"].get("hard_on", {}).get("mode") == "last_status":
                self.preferred = {"last": True}
            out["set_default_behavior"] = {"err_code": 0}
        if "set_preferred_state" in svc:
            self.preferred = svc["set_preferred_state"]
            out["set_preferred_state"] = {"err_code": 0}
        return {LIGHTING: out}

    def datagram_received(self, data, addr):
        self.received += 1
        if MIRROR["sock"] is not None:
            try:
                MIRROR["sock"].sendto(data, MIRROR["addr"])
                MIRROR["count"] += 1
            except OSError:
                pass
        if not self.online or self.rng.random() < self.loss:
            return
        try:
            cmd = kasa.unpack(data)
        except (ValueError, UnicodeDecodeError):
            return
        reply = kasa.pack(self.handle(cmd))
        if self.latency_ms > 0:
            delay = self.latency_ms / 1000 * self.rng.uniform(0.7, 1.3)
            asyncio.get_running_loop().call_later(delay, self._send, reply, addr)
        else:
            self._send(reply, addr)

    def _send(self, reply, addr):
        if self.transport and not self.transport.is_closing():
            self.transport.sendto(reply, addr)

    def rate(self, window=1.0):
        now = time.monotonic()
        return sum(1 for t in self.recent if now - t <= window) / window


class FakeBulbFleet:
    def __init__(self, count, base_ip="127.0.0.10", port=kasa.KASA_PORT,
                 latency_ms=0.0, loss=0.0, seed=1):
        a, b, c, d = (int(x) for x in base_ip.split("."))
        rng = random.Random(seed)
        self.bulbs = [FakeBulb(i, f"{a}.{b}.{c}.{d + i}", rng, latency_ms, loss, port)
                      for i in range(count)]
        self.port = port

    async def start(self):
        loop = asyncio.get_running_loop()
        for b in self.bulbs:
            await loop.create_datagram_endpoint(lambda b=b: b, local_addr=(b.ip, self.port))
        return self

    def stop(self):
        for b in self.bulbs:
            if b.transport:
                b.transport.close()

    def by_ip(self, ip):
        return next(b for b in self.bulbs if b.ip == ip)

    @property
    def ips(self):
        return [b.ip for b in self.bulbs]


async def console(fleet):
    import os
    import stat
    mode = os.fstat(sys.stdin.fileno()).st_mode
    if not (sys.stdin.isatty() or stat.S_ISFIFO(mode) or stat.S_ISSOCK(mode)):
        await asyncio.Event().wait()     # no console (e.g. under systemd): run until killed
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader()
    await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
    while True:
        line = (await reader.readline()).decode().split()
        if not line:
            if reader.at_eof():
                await asyncio.Event().wait()  # no console (e.g. run in the background): run until killed
            continue
        cmd, args = line[0], line[1:]
        try:
            if cmd in ("offline", "online"):
                fleet.bulbs[int(args[0])].online = cmd == "online"
            elif cmd == "latency":
                for b in ([fleet.bulbs[int(args[0])]] if len(args) > 1 else fleet.bulbs):
                    b.latency_ms = float(args[-1])
            elif cmd == "loss":
                for b in ([fleet.bulbs[int(args[0])]] if len(args) > 1 else fleet.bulbs):
                    b.loss = float(args[-1]) / 100
            elif cmd == "mirror":
                print(f"mirrored {MIRROR['count']} packets to {MIRROR['addr']}")
            elif cmd == "stats":
                for b in fleet.bulbs:
                    s = b.state
                    print(f"{b.index:3d} {b.ip:15s} {'up  ' if b.online else 'DOWN'} cmds {b.commands:7d} "
                          f"rate {b.rate():5.1f}/s  h{s['hue']:3d} s{s['saturation']:3d} "
                          f"v{s['brightness']:3d} on{s['on_off']}")
            elif cmd == "quit":
                return
            else:
                print("commands: offline N | online N | latency [N] MS | loss [N] PCT | stats | quit")
        except (IndexError, ValueError):
            print("bad arguments")


async def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--count", type=int, default=32)
    ap.add_argument("--base-ip", default="127.0.0.10")
    ap.add_argument("--port", type=int, default=kasa.KASA_PORT)
    ap.add_argument("--latency", type=float, default=5.0, help="reply latency in ms (+-30%%)")
    ap.add_argument("--loss", type=float, default=0.0, help="percent of packets dropped")
    ap.add_argument("--mirror", metavar="HOST[:PORT]", help="also send a copy of every packet there (default port 9)")
    ap.add_argument("--mirror-tos", type=lambda x: int(x, 0), default=0,
                    help="IP TOS byte for mirrored packets (0xC0 = WiFi voice), to match the engine's setting")
    args = ap.parse_args()
    if args.mirror:
        import socket
        MIRROR["sock"] = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        MIRROR["sock"].setblocking(False)
        if args.mirror_tos:
            MIRROR["sock"].setsockopt(socket.IPPROTO_IP, socket.IP_TOS, args.mirror_tos)
        host, _, port = args.mirror.partition(":")
        MIRROR["addr"] = (host, int(port or 9))
    fleet = await FakeBulbFleet(args.count, args.base_ip, args.port, args.latency, args.loss / 100).start()
    print(f"{args.count} fake bulbs on {fleet.ips[0]}..{fleet.ips[-1]} port {args.port}", flush=True)
    print(json.dumps({b.ip: b.mac for b in fleet.bulbs[:3]}), "...", flush=True)
    try:
        await console(fleet)
    finally:
        fleet.stop()


if __name__ == "__main__":
    asyncio.run(main())
