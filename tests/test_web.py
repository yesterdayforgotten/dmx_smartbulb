"""API tests: the real engine with fake bulbs and replayed DMX, in one event loop."""

import asyncio
import json

import httpx
import pytest

pytest.importorskip("fastapi")

from engine.config import ConfigStore, validate   # noqa: E402
from engine.core import Engine                     # noqa: E402
from engine.receiver import Receiver               # noqa: E402
from engine.web import create_app                  # noqa: E402
from tests.test_engine import recording            # noqa: E402
from tools.fake_bulbs import FakeBulbFleet         # noqa: E402

PORT = 19995


class Harness:
    def __init__(self, tmp_path, n=3):
        self.tmp_path, self.n = tmp_path, n

    async def __aenter__(self):
        self.fleet = await FakeBulbFleet(self.n, port=PORT).start()
        bulbs = {b.mac: {"name": f"b{i}", "ip": b.ip, "channel": 1 + 3 * i} for i, b in enumerate(self.fleet.bulbs)}
        cfg = validate({"bulbs": bulbs, "kasa_port": PORT, "web_port": 8080})
        self.store = ConfigStore(self.tmp_path / "config.json")
        self.store.save(cfg)
        self.engine = Engine(self.store, cfg, receiver=Receiver("replay", str(recording(self.tmp_path))),
                             discovery_targets=self.fleet.ips)
        self.task = asyncio.create_task(self.engine.run())
        await asyncio.sleep(0.3)
        self.app = create_app(self.engine, firmware_dir=self.tmp_path / "fw")
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://pi")
        return self

    async def __aexit__(self, *exc):
        await self.client.aclose()
        self.engine._stopping = True
        await self.task
        self.fleet.stop()

    async def login(self, pw="secret123"):
        r = await self.client.post("/api/setup", json={"password": pw})
        assert r.status_code == 200, r.text
        self.client.cookies.update(r.cookies)


def run(coro):
    asyncio.run(coro)


def test_first_run_setup_then_login(tmp_path):
    async def go():
        async with Harness(tmp_path) as h:
            c = h.client
            assert (await c.get("/api/session")).json() == {"setup_needed": True, "authenticated": False}
            assert (await c.get("/api/state")).status_code == 401
            assert (await c.post("/api/setup", json={"password": "abc"})).status_code == 400   # too short
            r = await c.post("/api/setup", json={"password": "secret123", "ssid": "ShowNet", "wifi_password": "pw"})
            assert r.status_code == 200 and "dmxs" in r.cookies
            assert (await c.post("/api/setup", json={"password": "other123"})).status_code == 403
            c.cookies.update(r.cookies)
            st = (await c.get("/api/state")).json()
            assert "auth" not in st["config"] and st["config"]["network"]["password"] == "********"
            c.cookies.clear()
            assert (await c.post("/api/login", json={"password": "nope"})).status_code == 401
            r = await c.post("/api/login", json={"password": "secret123"})
            assert r.status_code == 200
    run(go())


def test_password_change_logs_out_old_sessions(tmp_path):
    async def go():
        async with Harness(tmp_path) as h:
            await h.login()
            old = dict(h.client.cookies)
            r = await h.client.post("/api/password", json={"current": "secret123", "new": "newpass99"})
            assert r.status_code == 200
            stale = httpx.AsyncClient(transport=httpx.ASGITransport(app=h.app), base_url="http://pi", cookies=old)
            assert (await stale.get("/api/state")).status_code == 401
            await stale.aclose()
    run(go())


def test_patch_edit_conflicts_and_auto_assign(tmp_path):
    async def go():
        async with Harness(tmp_path) as h:
            await h.login()
            macs = list(h.engine.cfg["bulbs"])
            r = await h.client.patch(f"/api/bulbs/{macs[1]}", json={"channel": 2})
            assert r.status_code == 200
            st = (await h.client.get("/api/state")).json()
            assert st["warnings"] and "channel 2" in st["warnings"][0]
            assert (await h.client.patch(f"/api/bulbs/{macs[1]}", json={"channel": 511})).status_code == 400
            r = await h.client.post("/api/bulbs/auto-assign", json={"macs": macs, "start": 100})
            assert r.status_code == 200
            chans = [h.engine.cfg["bulbs"][m]["channel"] for m in macs]
            assert chans == [100, 103, 106]
            assert ConfigStore(tmp_path / "config.json").load()["bulbs"][macs[2]]["channel"] == 106
    run(go())


def test_groups_follow(tmp_path):
    async def go():
        async with Harness(tmp_path) as h:
            await h.login()
            mac = list(h.engine.cfg["bulbs"])[0]
            assert (await h.client.put("/api/groups/Uplights", json={"channel": 200})).status_code == 200
            assert (await h.client.patch(f"/api/bulbs/{mac}", json={"follow": "Uplights", "groups": ["Uplights"]})).status_code == 200
            b = h.engine.cfg["bulbs"][mac]
            assert b["follow"] == "Uplights" and b["channel"] is None
            assert h.engine.sender.bulbs[mac].channel == 200
            assert (await h.client.delete("/api/groups/Uplights")).status_code == 200
            assert h.engine.cfg["bulbs"][mac]["follow"] is None
    run(go())


def test_control_look_and_recall(tmp_path):
    async def go():
        async with Harness(tmp_path) as h:
            await h.login()
            macs = list(h.engine.cfg["bulbs"])
            r = await h.client.post("/api/control", json={"macs": macs[:2], "state": {"k": 2700, "v": 60}})
            assert r.status_code == 200
            await asyncio.sleep(0.3)
            assert h.fleet.bulbs[0].state["color_temp"] == 2700
            r = await h.client.post("/api/looks", json={"name": "House"})
            assert r.status_code == 200 and r.json()["bulbs"] == 3
            assert h.engine.cfg["looks"]["House"][macs[0]] == {"k": 2700, "v": 60}
            assert (await h.client.post("/api/looks/House/recall")).status_code == 200
            assert (await h.client.post("/api/looks/Nope/recall")).status_code == 404
            assert (await h.client.delete("/api/looks/House")).status_code == 200
    run(go())


def test_discover_add_remove(tmp_path):
    async def go():
        async with Harness(tmp_path) as h:
            await h.login()
            mac = list(h.engine.cfg["bulbs"])[2]
            assert (await h.client.delete(f"/api/bulbs/{mac}")).status_code == 200
            found = (await h.client.post("/api/discover")).json()["found"]
            new = [f for f in found if not f["known"]]
            assert [f["mac"] for f in new] == [mac]
            r = await h.client.post("/api/bulbs", json={"mac": mac, "ip": new[0]["ip"], "name": "back", "channel": "next"})
            assert r.status_code == 200 and h.engine.cfg["bulbs"][mac]["channel"] == 7
    run(go())


def test_power_on_and_identify(tmp_path):
    async def go():
        async with Harness(tmp_path) as h:
            await h.login()
            macs = list(h.engine.cfg["bulbs"])
            r = await h.client.post("/api/power-on", json={"macs": macs, "state": {"k": 2700, "v": 80}})
            assert all(r.json()["results"].values())
            assert h.fleet.bulbs[0].preferred["brightness"] == 80
            before = h.fleet.bulbs[0].commands
            assert (await h.client.post(f"/api/bulbs/{macs[0]}/identify")).status_code == 200
            await asyncio.sleep(1.0)
            assert h.fleet.bulbs[0].commands > before + 2
    run(go())


def test_settings_backup_restore(tmp_path):
    async def go():
        async with Harness(tmp_path) as h:
            await h.login()
            r = await h.client.put("/api/settings", json={"sender": {"curve": "square", "min_interval_ms": 100}})
            assert r.status_code == 200 and h.engine.sender.min_interval == 0.1
            assert (await h.client.put("/api/settings", json={"sender": {"curve": "wobbly"}})).status_code == 400
            assert (await h.client.put("/api/settings", json={"auth": {}})).status_code == 400
            backup = (await h.client.get("/api/backup")).json()
            assert backup["sender"]["curve"] == "square"
            backup["sender"]["curve"] = "linear"
            r = await h.client.post("/api/restore", json=backup)
            assert r.status_code == 200 and h.engine.cfg["sender"]["curve"] == "linear"
    run(go())


def test_live_websocket(tmp_path):
    """The WebSocket needs a real server; run uvicorn in the same loop."""
    import uvicorn
    import websockets

    async def go():
        async with Harness(tmp_path) as h:
            await h.login()
            server = uvicorn.Server(uvicorn.Config(h.app, host="127.0.0.1", port=18089, log_level="warning", lifespan="off"))
            task = asyncio.create_task(server.serve())
            await asyncio.sleep(0.5)
            try:
                with pytest.raises(Exception):
                    async with websockets.connect("ws://127.0.0.1:18089/api/live") as ws:
                        await ws.recv()            # no cookie: closed
                cookie = "; ".join(f"{k}={v}" for k, v in h.client.cookies.items())
                async with websockets.connect("ws://127.0.0.1:18089/api/live",
                                              additional_headers={"Cookie": cookie}) as ws:
                    msgs = [json.loads(await ws.recv()) for _ in range(3)]
                assert msgs[-1]["online"] == 3 and len(msgs[-1]["bulbs"]) == 3
                assert msgs[-1]["input"]["frames"] >= 0
            finally:
                server.should_exit = True
                await task
    run(go())


def test_firmware_served_publicly_and_update_refused_while_dmx_live(tmp_path):
    async def go():
        async with Harness(tmp_path) as h:
            await h.login()
            assert (await h.client.get("/firmware/nothing.bin")).status_code == 404
            mac = list(h.engine.cfg["bulbs"])[0]
            await asyncio.sleep(0.5)                  # replayed DMX is flowing
            r = await h.client.post(f"/api/bulbs/{mac}/firmware", json={})
            assert r.status_code == 409 and "DMX is live" in r.json()["detail"]
    run(go())


def test_dmx_can_be_ignored_and_restored(tmp_path):
    async def go():
        async with Harness(tmp_path) as h:
            await h.login()
            await asyncio.sleep(0.6)                       # replayed DMX has set the bulbs
            r = await h.client.post("/api/dmx", json={"enabled": False})
            assert r.json()["enabled"] is False
            mac = list(h.engine.cfg["bulbs"])[0]
            await h.client.post("/api/control", json={"macs": [mac], "state": {"k": 2700, "v": 40}})
            await asyncio.sleep(0.5)                       # DMX keeps flowing but must not override
            assert h.engine.sender.bulbs[mac].target == ("temp", 2700, 40)
            st = (await h.client.get("/api/state")).json()
            assert st["live"]["dmx_enabled"] is False and st["live"]["input"]["frames"] > 0
            await h.client.post("/api/dmx", json={"enabled": True})
            await asyncio.sleep(0.3)
            assert h.engine.dmx_enabled
    run(go())


def test_board_show_starts_from_the_api(tmp_path, monkeypatch):
    """Show endpoints must run in the engine loop (they create an asyncio task)."""
    async def go():
        async with Harness(tmp_path) as h:
            await h.login()
            monkeypatch.setattr(h.engine.board, "start", lambda: None)
            r = await h.client.post("/api/board/show", json={"name": "rainbow", "speed": 2})
            assert r.status_code == 200, r.text
            assert h.engine.board.show == "rainbow" and h.engine.board.speed == 2
            r = await h.client.post("/api/board/show", json={"name": "rainbow", "speed": 3})
            assert r.status_code == 200 and h.engine.board.speed == 3
            assert (await h.client.post("/api/board/stop-show")).status_code == 200
            assert h.engine.board.show is None
    run(go())
