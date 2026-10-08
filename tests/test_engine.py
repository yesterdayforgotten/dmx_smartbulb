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


def test_idle_bulbs_show_online(tmp_path):
    """With no DMX and nothing sent, status queries still mark bulbs online."""
    async def go():
        fleet = await FakeBulbFleet(2, port=PORT).start()
        try:
            engine = make_engine(tmp_path, fleet, recording(tmp_path))
            await engine.start()
            engine.receiver.stop()                       # no DMX at all
            engine._poll_quiet_bulbs(time.monotonic())
            await asyncio.sleep(0.2)
            assert engine.status()["online"] == 2
            assert all(b.commands == 0 for b in fleet.bulbs)   # nothing changed on the bulbs
            fleet.bulbs[1].online = False
            for rt in engine.sender.bulbs.values():
                rt.last_reply -= 10
                rt.last_poll = -1e9
            engine._poll_quiet_bulbs(time.monotonic())
            await asyncio.sleep(0.2)
            assert engine.status()["online"] == 1
            engine.stop()
        finally:
            fleet.stop()
    asyncio.run(go())


def test_bulb_info_and_power_on_are_read(tmp_path):
    """Discovery records model/firmware/signal; power-on defaults are read back,
    including right after they're changed."""
    async def go():
        fleet = await FakeBulbFleet(2, port=PORT).start()
        try:
            engine = make_engine(tmp_path, fleet, recording(tmp_path))
            await engine.start()
            await engine.rediscover()
            await engine.read_power_on()
            mac = fleet.bulbs[0].mac
            assert engine.info[mac]["model"] == "KL135(US)" and engine.info[mac]["fw"].startswith("1.0")
            assert engine.info[mac]["power_on"]["mode"] == "preset"
            await engine.set_power_on([mac], ("temp", 3000, 60))
            assert engine.info[mac]["power_on"]["k"] == 3000 and engine.info[mac]["power_on"]["v"] == 60
            assert "info" in engine.status()
            engine.stop()
        finally:
            fleet.stop()
    asyncio.run(go())


def test_power_draw_colour_and_last_state_power_on(tmp_path):
    async def go():
        fleet = await FakeBulbFleet(2, port=PORT).start()
        try:
            engine = make_engine(tmp_path, fleet, recording(tmp_path))
            await engine.start()
            engine.receiver.stop()
            mac0, mac1 = fleet.bulbs[0].mac, fleet.bulbs[1].mac
            engine._poll_quiet_bulbs(time.monotonic())        # the idle check carries the power reading
            await asyncio.sleep(0.2)
            assert engine.info[mac0]["power_mw"] > 0 and engine.info[mac0]["lumens"] is not None
            assert all(b.commands == 0 for b in fleet.bulbs)  # still nothing changed on the bulbs
            ok = await engine.set_power_on([mac0], ("hsv", 240, 100, 50))
            assert ok[mac0] and engine.info[mac0]["power_on"] == {"mode": "preset", "h": 240, "s": 100, "k": 0, "v": 50}
            ok = await engine.set_power_on([mac1], ("last",))
            assert ok[mac1] and engine.info[mac1]["power_on"] == {"mode": "last"}
            engine.stop()
        finally:
            fleet.stop()
    asyncio.run(go())


def test_wifi_priority_marks_packets(tmp_path):
    import socket as so
    async def go():
        fleet = await FakeBulbFleet(1, port=PORT).start()
        try:
            engine = make_engine(tmp_path, fleet, recording(tmp_path))
            await engine.start()
            sock = engine.transport.transport.get_extra_info("socket")
            assert sock.getsockopt(so.IPPROTO_IP, so.IP_TOS) == 0
            engine.update_config(lambda c: c["sender"].update(wifi_priority="voice"))
            assert sock.getsockopt(so.IPPROTO_IP, so.IP_TOS) == 0xC0
            engine.stop()
        finally:
            fleet.stop()
    asyncio.run(go())
