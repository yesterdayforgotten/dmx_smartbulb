"""End to end: replayed DMX -> receiver process -> engine -> fake KL135 bulbs."""

import asyncio
import time

from engine.config import ConfigStore, validate
from engine.core import Engine
from engine.receiver import Receiver
from engine import kasa
from tools.fake_bulbs import FakeBulbFleet

PORT = 19998


def recording(tmp_path, hue=85, sat=255, val=255, n=400):
    """A recording where channels 1-3 and 4-6 hold a fixed colour."""
    from tools.dmx_testpattern import encode_parmrk
    slots = bytearray(512)
    slots[0:3] = bytes([hue, sat, val])
    slots[3:6] = bytes([170, 255, 128])
    path = tmp_path / "rec.bin"
    path.write_bytes(encode_parmrk(0, bytes(slots)) * n + b"\xff\x00\x00")
    return path


async def run_for(engine, seconds):
    task = asyncio.create_task(engine.run())
    await asyncio.sleep(seconds)
    engine._stopping = True
    await task


def make_engine(tmp_path, fleet, rec, **cfg_extra):
    bulbs = {b.mac: {"name": f"b{i}", "ip": b.ip, "channel": 1 if i < 2 else 4}
             for i, b in enumerate(fleet.bulbs)}
    cfg = validate({"bulbs": bulbs, "kasa_port": PORT, **cfg_extra})
    store = ConfigStore(tmp_path / "config.json")
    store.save(cfg)
    return Engine(store, cfg, receiver=Receiver("replay", str(rec)), discovery_targets=fleet.ips)


def test_bulbs_follow_dmx(tmp_path):
    async def go():
        fleet = await FakeBulbFleet(3, port=PORT).start()
        try:
            engine = make_engine(tmp_path, fleet, recording(tmp_path))
            await run_for(engine, 1.5)
            b0, b1, b2 = fleet.bulbs
            assert (b0.state["hue"], b0.state["saturation"], b0.state["brightness"]) == (120, 100, 100)
            assert b1.state["hue"] == 120                       # shares channel 1
            assert (b2.state["hue"], b2.state["brightness"]) == (240, 50)
            st = engine.status()
            assert st["online"] == 3 and st["input"]["frames"] > 0
            assert st["sender"]["latency_p95_ms"] is not None
        finally:
            fleet.stop()
    asyncio.run(go())


def test_ip_change_found_by_mac(tmp_path):
    async def go():
        fleet = await FakeBulbFleet(3, port=PORT).start()
        try:
            engine = make_engine(tmp_path, fleet, recording(tmp_path))
            mac = fleet.bulbs[2].mac
            engine.cfg["bulbs"][mac]["ip"] = "127.0.0.99"       # the config has a stale IP
            await engine.start()
            engine.sender.apply_config(engine.cfg)
            await engine.rediscover()
            assert engine.cfg["bulbs"][mac]["ip"] == fleet.bulbs[2].ip
            assert ConfigStore(tmp_path / "config.json").load()["bulbs"][mac]["ip"] == fleet.bulbs[2].ip
            assert engine.ip_changes and engine.ip_changes[-1][3] == fleet.bulbs[2].ip
            engine.stop()
        finally:
            fleet.stop()
    asyncio.run(go())


def test_dmx_loss_blackout(tmp_path):
    async def go():
        fleet = await FakeBulbFleet(1, port=PORT).start()
        try:
            rec = recording(tmp_path, n=20)                     # a short recording...
            engine = make_engine(tmp_path, fleet, rec, dmx_loss={"mode": "blackout", "after_s": 1.0})
            await engine.start()
            engine.receiver.stop()                              # ...then the input goes away
            t0 = time.monotonic()
            # Feed one frame by hand, then nothing.
            engine.last_frame = t0
            engine.sender.update_dmx(bytes([85, 255, 255]) + bytes(509), t0, t0)
            engine.sender.tick(t0)
            await asyncio.sleep(0.1)
            assert fleet.bulbs[0].state["brightness"] == 100
            engine._check_loss(t0 + 1.5)
            engine.sender.tick(t0 + 1.5)
            await asyncio.sleep(0.1)
            assert engine.loss_applied and fleet.bulbs[0].state["on_off"] == 0
            engine.stop()
        finally:
            fleet.stop()
    asyncio.run(go())
