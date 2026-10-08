"""Web UI and JSON API (FastAPI), served by the engine's own event loop.

Public: the static UI, /api/session, /api/setup (first run only), /api/login,
and /firmware/<file> (bulbs download firmware from here without a login).
Everything else needs the session cookie. /api/live is a WebSocket that pushes
the engine's status about ten times a second.
"""

import asyncio
import json
import socket
import time
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from engine import auth, firmware
from engine.config import FOOTPRINT, ConfigError, next_free_channel, patch_conflicts

STATIC = Path(__file__).resolve().parent.parent / "web" / "static"
COOKIE = "dmxs"
LIVE_HZ = 10


def to_state(d):
    """API color {h, s, v} or {k, v} to the sender's tuple."""
    try:
        if "k" in d:
            return ("temp", int(d["k"]), int(d["v"]))
        return ("hsv", int(d["h"]), int(d["s"]), int(d["v"]))
    except (KeyError, TypeError, ValueError):
        raise HTTPException(400, "color needs h, s, v or k, v")


def from_state(st):
    if st is None:
        return None
    if st[0] == "temp":
        return {"k": st[1], "v": st[2]}
    return {"h": st[1], "s": st[2], "v": st[3]}


def public_config(cfg):
    """The config without secrets."""
    out = json.loads(json.dumps(cfg))
    out.pop("auth", None)
    if out.get("network", {}).get("password"):
        out["network"]["password"] = "********"
    return out


def local_ip_for(peer_ip):
    """This Pi's address as seen from peer_ip (for URLs a bulb must reach)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((peer_ip, 9))
        return s.getsockname()[0]
    finally:
        s.close()


def create_app(engine, firmware_dir=firmware.DEFAULT_CACHE):
    app = FastAPI(title="dmx_smartbulb", docs_url=None, redoc_url=None, openapi_url=None)
    fw_jobs = {}        # mac -> {"state", "ratio", "message", "version"}
    failed_logins = {"t": 0.0}

    # ---- helpers -----------------------------------------------------------

    def authed(token):
        return auth.check_session(token, engine.cfg["auth"]["session_secret"])

    def need_auth(request: Request):
        if not authed(request.cookies.get(COOKIE)):
            raise HTTPException(401, "log in first")

    def change(fn):
        try:
            return engine.update_config(fn)
        except ConfigError as e:
            raise HTTPException(400, e.problems)

    def login_response(payload):
        resp = JSONResponse(payload)
        resp.set_cookie(COOKIE, auth.make_session(engine.cfg["auth"]["session_secret"]),
                        max_age=auth.SESSION_DAYS * 86400, httponly=True, samesite="strict")
        return resp

    def bulb_or_404(mac):
        if mac not in engine.cfg["bulbs"]:
            raise HTTPException(404, f"no bulb {mac}")
        return engine.cfg["bulbs"][mac]

    def live_payload():
        st = engine.status()
        bulbs = {}
        for mac, b in st["bulbs"].items():
            bulbs[mac] = {"state": from_state(b["state"]), "online": b["online"], "source": b["source"],
                          "backoff": b["backoff"], "rtt": b["rtt_ms"], "interval": b["interval_ms"],
                          "sends": b["sends"], "replies": b["replies"], "misses": b["misses"]}
        inp, snd = st["input"], st["sender"]
        return {"t": time.time(), "dmx": st["dmx_present"], "loss": st["dmx_lost_applied"],
                "dmx_enabled": st["dmx_enabled"], "last_frame_age_s": st["last_frame_age_s"],
                "input": {k: inp.get(k) for k in ("frames", "malformed", "error_bytes", "held", "oe", "fe",
                                                  "restarts", "alive", "slots", "other_sc")},
                "sender": {k: snd.get(k) for k in ("sent", "refreshes", "budget_waits", "latency_p50_ms",
                                                   "latency_p95_ms", "latency_max_ms", "latency_hist", "queued_p95_ms", "mode", "frame_period_ms",
                                                   "delivery_spread_p95_ms", "reply_spread_p95_ms")},
                "online": st["online"], "bulbs": bulbs, "ip_changes": st["ip_changes"][-5:],
                "firmware": fw_jobs,
                "board": {"available": engine.board.available(), "active": engine.board.active,
                          "show": engine.board.show, "frames": engine.board.frames, "error": engine.board.error}}

    # ---- session -----------------------------------------------------------

    @app.get("/api/session")
    def session(request: Request):
        return {"setup_needed": not engine.cfg["auth"]["password_hash"],
                "authenticated": authed(request.cookies.get(COOKIE)),
                "title": engine.cfg["ui"]["title"]}

    @app.post("/api/setup")
    def setup(body: dict = Body(...)):
        if engine.cfg["auth"]["password_hash"]:
            raise HTTPException(403, "already set up; log in instead")
        pw = body.get("password", "")
        try:
            def apply(c):
                auth.set_password(c, pw)
                if body.get("ssid"):
                    c["network"]["ssid"] = body["ssid"]
                    c["network"]["password"] = body.get("wifi_password", "")
            change(apply)
        except ValueError as e:
            raise HTTPException(400, str(e))
        return login_response({"ok": True})

    @app.post("/api/login")
    async def login(body: dict = Body(...)):
        stored = engine.cfg["auth"]["password_hash"]
        if not stored or not auth.check_password(body.get("password", ""), stored):
            # Slow down guessing a little.
            await asyncio.sleep(1.0)
            raise HTTPException(401, "wrong password")
        return login_response({"ok": True})

    @app.post("/api/logout")
    def logout():
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(COOKIE)
        return resp

    @app.post("/api/password")
    def password(request: Request, body: dict = Body(...)):
        need_auth(request)
        if not auth.check_password(body.get("current", ""), engine.cfg["auth"]["password_hash"]):
            raise HTTPException(400, "the current password is wrong")
        try:
            change(lambda c: auth.set_password(c, body.get("new", "")))
        except ValueError as e:
            raise HTTPException(400, str(e))
        return login_response({"ok": True})     # other sessions are logged out

    # ---- state -------------------------------------------------------------

    @app.get("/api/state")
    def state(request: Request):
        need_auth(request)
        return {"config": public_config(engine.cfg), "warnings": patch_conflicts(engine.cfg),
                "next_free_channel": next_free_channel(engine.cfg), "live": live_payload(),
                "info": engine.info,
                "firmware_images": firmware.cached(firmware_dir)}

    @app.websocket("/api/live")
    async def live(ws: WebSocket):
        if not authed(ws.cookies.get(COOKIE)):
            await ws.close(code=4401)
            return
        await ws.accept()
        try:
            while True:
                await ws.send_text(json.dumps(live_payload()))
                await asyncio.sleep(1 / LIVE_HZ)
        except (WebSocketDisconnect, RuntimeError):
            pass

    # ---- bulbs -------------------------------------------------------------

    @app.post("/api/discover")
    async def discover(request: Request):
        need_auth(request)
        return {"found": await engine.find_bulbs()}

    @app.post("/api/bulbs")
    def add_bulb(request: Request, body: dict = Body(...)):
        need_auth(request)
        mac = (body.get("mac") or "").upper()
        if mac in engine.cfg["bulbs"]:
            raise HTTPException(400, "that bulb is already added")
        channel = body.get("channel")
        if channel == "next":
            channel = next_free_channel(engine.cfg)
        change(lambda c: c["bulbs"].__setitem__(mac, {"name": body.get("name") or mac, "ip": body.get("ip"),
                                                      "channel": channel}))
        return {"ok": True, "mac": mac}

    @app.patch("/api/bulbs/{mac}")
    async def edit_bulb(mac: str, request: Request, body: dict = Body(...)):
        need_auth(request)
        bulb_or_404(mac)
        allowed = {"name", "channel", "follow", "dmx", "groups", "pos", "ip", "mode"}
        unknown = set(body) - allowed
        if unknown:
            raise HTTPException(400, f"can't change {', '.join(sorted(unknown))}")

        def apply(c):
            b = c["bulbs"][mac]
            b.update(body)
            if "channel" in body and body["channel"] is not None:
                b["follow"] = None
            if "follow" in body and body["follow"] is not None:
                b["channel"] = None
        change(apply)
        if "name" in body and str(body["name"]).strip():
            return {"ok": True, "device": await engine.rename_device(mac, str(body["name"]).strip())}
        return {"ok": True}

    @app.delete("/api/bulbs/{mac}")
    def remove_bulb(mac: str, request: Request):
        need_auth(request)
        bulb_or_404(mac)

        def apply(c):
            del c["bulbs"][mac]
            for look in c["looks"].values():
                look.pop(mac, None)
        change(apply)
        return {"ok": True}

    @app.post("/api/bulbs/auto-assign")
    def auto_assign(request: Request, body: dict = Body(...)):
        """Give the listed bulbs consecutive 3-channel addresses from `start`."""
        need_auth(request)
        macs, start = body.get("macs") or [], int(body.get("start") or 1)

        def apply(c):
            for mac in macs:
                c["bulbs"][mac]["channel"] = None
                c["bulbs"][mac]["follow"] = None
            ch = start
            for mac in macs:
                size = FOOTPRINT[c["bulbs"][mac]["mode"]]
                ch = next_free_channel(c, ch, size)
                if ch is None:
                    raise ConfigError(["not enough free channels"])
                c["bulbs"][mac]["channel"] = ch
                ch += size
        change(apply)
        return {"ok": True}

    @app.post("/api/bulbs/{mac}/identify")
    async def identify(mac: str, request: Request):
        need_auth(request)
        bulb_or_404(mac)
        asyncio.create_task(engine.identify(mac))
        return {"ok": True}

    # ---- groups ------------------------------------------------------------

    @app.put("/api/groups/{name}")
    def put_group(name: str, request: Request, body: dict = Body(...)):
        need_auth(request)
        change(lambda c: c["groups"].__setitem__(name.strip(), {"channel": body.get("channel")}))
        return {"ok": True}

    @app.delete("/api/groups/{name}")
    def delete_group(name: str, request: Request):
        need_auth(request)

        def apply(c):
            c["groups"].pop(name, None)
            for b in c["bulbs"].values():
                b["groups"] = [g for g in b["groups"] if g != name]
                if b["follow"] == name:
                    b["follow"] = None
        change(apply)
        return {"ok": True}

    @app.post("/api/dmx")
    def dmx_toggle(request: Request, body: dict = Body(...)):
        need_auth(request)
        engine.set_dmx_enabled(body.get("enabled", True))
        return {"ok": True, "enabled": engine.dmx_enabled}

    # ---- control and looks ---------------------------------------------------

    @app.post("/api/control")
    def control(request: Request, body: dict = Body(...)):
        need_auth(request)
        engine.set_color(body.get("macs") or [], to_state(body.get("state") or {}))
        return {"ok": True}

    @app.post("/api/looks")
    def save_look(request: Request, body: dict = Body(...)):
        need_auth(request)
        name = (body.get("name") or "").strip()
        if not name:
            raise HTTPException(400, "give the look a name")
        snap = engine.sender.snapshot_states(set(body["macs"]) if body.get("macs") else None)
        if not snap:
            raise HTTPException(400, "no bulb has a color yet")
        change(lambda c: c["looks"].__setitem__(name, snap))
        return {"ok": True, "bulbs": len(snap)}

    @app.post("/api/looks/{name}/recall")
    def recall_look(name: str, request: Request):
        need_auth(request)
        if name not in engine.cfg["looks"]:
            raise HTTPException(404, f"no look {name!r}")
        engine.recall_look(name)
        return {"ok": True}

    @app.delete("/api/looks/{name}")
    def delete_look(name: str, request: Request):
        need_auth(request)

        def apply(c):
            c["looks"].pop(name, None)
            if c["dmx_loss"]["look"] == name:
                c["dmx_loss"].update(mode="hold", look=None)
        change(apply)
        return {"ok": True}

    @app.post("/api/power-on")
    async def power_on(request: Request, body: dict = Body(...)):
        """state: {"k", "v"} (white), {"h", "s", "v"} (color) or {"mode": "last"}."""
        need_auth(request)
        st = body.get("state") or {}
        state = ("last",) if st.get("mode") == "last" else to_state(st)
        results = await engine.set_power_on(body.get("macs") or [], state)
        return {"results": results}

    # ---- settings, backup ------------------------------------------------------

    @app.put("/api/settings")
    def settings(request: Request, body: dict = Body(...)):
        need_auth(request)
        allowed = {"sender", "dmx_loss", "input", "network", "identify", "ui"}
        if set(body) - allowed:
            raise HTTPException(400, f"settings can only change {', '.join(sorted(allowed))}")
        old_input = dict(engine.cfg["input"])

        def apply(c):
            for section, values in body.items():
                if section == "network" and values.get("password") == "********":
                    values = {k: v for k, v in values.items() if k != "password"}
                c[section].update(values)
        change(apply)
        restart = engine.cfg["input"] != old_input
        if restart:
            engine.restart_receiver()
        return {"ok": True, "input_restarted": restart}

    @app.get("/api/backup")
    def backup(request: Request):
        need_auth(request)
        name = f"dmx_smartbulb-config-{time.strftime('%Y%m%d-%H%M')}.json"
        return Response(json.dumps(engine.cfg, indent=1, sort_keys=True), media_type="application/json",
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})

    @app.post("/api/restore")
    def restore(request: Request, body: dict = Body(...)):
        need_auth(request)
        cfg = body.get("config", body)          # accept a backup or a saved-file envelope
        keep_auth = engine.cfg["auth"]
        if not cfg.get("auth", {}).get("password_hash"):
            cfg["auth"] = keep_auth             # a backup without a password keeps the current one
        change(lambda c: (c.clear(), c.update(cfg)))
        return {"ok": True, "bulbs": len(engine.cfg["bulbs"])}

    # ---- control board (DMX out through an Enttec) ---------------------------------

    @app.get("/api/board")
    async def board_state(request: Request):
        need_auth(request)
        groups = {name: len(engine.group_fixtures(name)) for name in engine.cfg["groups"]}
        return {**engine.board.status(), "fixtures": engine.board.patched_values(), "groups": groups}

    def board_running():
        try:
            engine.board.start()
        except RuntimeError as e:
            raise HTTPException(409, str(e))

    @app.post("/api/board/channels")
    async def board_channels(request: Request, body: dict = Body(...)):
        need_auth(request)
        board_running()
        engine.board.set_channels(body.get("values") or {})
        return {"ok": True}

    @app.post("/api/board/color")
    async def board_color(request: Request, body: dict = Body(...)):
        """Set fixtures to one color: {channels: [...], h, s, v} or {channels, k, v}."""
        need_auth(request)
        board_running()
        chans = [int(c) for c in body.get("channels") or []]
        if "k" in body:
            engine.board.set_fixture_white(chans, float(body["k"]), int(body.get("v", 100)))
        else:
            engine.board.set_fixture_color(chans, int(body.get("h", 0)), int(body.get("s", 0)), int(body.get("v", 0)))
        return {"ok": True}

    @app.post("/api/board/show")
    async def board_show(request: Request, body: dict = Body(...)):
        need_auth(request)
        board_running()
        target = body.get("target") or "all"
        channels = None
        if target != "all":
            if target not in engine.cfg["groups"]:
                raise HTTPException(400, f"no group {target!r}")
            channels = engine.group_fixtures(target)
            if not channels:
                raise HTTPException(400, f"group {target} has no patched fixtures")
        try:
            rid = engine.board.start_show(body.get("name"), body.get("speed", 1.0), target, channels)
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"ok": True, "id": rid}

    @app.post("/api/board/stop-show")
    async def board_stop_show(request: Request, body: dict = Body(default={})):
        """Stop one show ({id}) or all of them."""
        need_auth(request)
        engine.board.stop_show(body.get("id"))
        return {"ok": True}

    @app.post("/api/board/blackout")
    async def board_blackout(request: Request):
        need_auth(request)
        board_running()
        engine.board.blackout()
        return {"ok": True}

    @app.post("/api/board/release")
    async def board_release(request: Request):
        need_auth(request)
        engine.board.stop_show()                    # the show task lives in this loop
        await asyncio.to_thread(engine.board.release)   # joins the output thread
        return {"ok": True}

    # ---- firmware ----------------------------------------------------------------

    @app.get("/firmware/{file}")
    def firmware_file(file: str):
        names = {img["file"] for img in firmware.cached(firmware_dir)}
        if file not in names:
            raise HTTPException(404, "no such firmware")
        return FileResponse(Path(firmware_dir) / file, media_type="application/octet-stream")

    @app.post("/api/bulbs/{mac}/firmware")
    async def update_firmware(mac: str, request: Request, body: dict = Body(default={})):
        need_auth(request)
        bulb_or_404(mac)
        if engine.dmx_present(time.monotonic()) and not body.get("force"):
            raise HTTPException(409, "DMX is live; updating would black out this bulb mid-show")
        if fw_jobs.get(mac, {}).get("state") == "running":
            raise HTTPException(409, "already updating this bulb")
        info = await engine.bulb_info(mac)
        if not info:
            raise HTTPException(400, "the bulb isn't answering")
        img = firmware.image_for(info.get("model"), info.get("hw_ver"), firmware_dir)
        if not img:
            raise HTTPException(400, f"no firmware on this Pi for {info.get('model')} hw {info.get('hw_ver')}")
        ip = engine.cfg["bulbs"][mac]["ip"]
        url = f"http://{local_ip_for(ip)}:{engine.cfg['web_port']}/firmware/{img['file']}"
        fw_jobs[mac] = {"state": "running", "ratio": 0, "message": f"{info.get('sw_ver')} -> {img['version']}",
                        "version": None}

        async def job():
            def progress(ratio, status):
                fw_jobs[mac]["ratio"] = ratio
            try:
                v = await engine.update_firmware(mac, url, progress)
                fw_jobs[mac].update(state="done", ratio=100, version=v, message=f"now {v}")
            except Exception as e:  # report any failure to the UI
                fw_jobs[mac].update(state="failed", message=str(e))
        asyncio.create_task(job())
        return {"ok": True, "url": url}

    # ---- static UI -----------------------------------------------------------------

    if STATIC.is_dir():
        app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")
    return app
