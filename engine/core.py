"""The engine: receiver -> sender -> bulbs, plus DMX-loss handling and finding
bulbs again by MAC when their IP changes.
"""

import asyncio
import logging
import time

from engine import kasa
from engine.config import FOOTPRINT, group_size
from engine.receiver import Receiver
from engine.sender import HIST_EDGES_MS, Sender, histogram, percentile

log = logging.getLogger("engine")

LOSS_DETECT_S = 1.0          # no frame for this long means DMX is lost
TICK_S = 0.01                # tick at least this often without new frames
REDISCOVER_AFTER_S = 10.0    # a bulb offline this long triggers rediscovery
REDISCOVER_EVERY_S = 30.0    # at most this often
INFO_EVERY_S = 600.0         # refresh firmware/model/signal and power-on defaults this often


class Engine:
    def __init__(self, store, cfg, receiver=None, discover=kasa.discover, discovery_targets=None):
        """store: ConfigStore (saved when a bulb's IP changes). receiver: a
        Receiver (made from the config if omitted). discover and
        discovery_targets let tests point discovery at fake bulbs."""
        self.store = store
        self.cfg = cfg
        inp = cfg["input"]
        self.receiver = receiver or Receiver(inp["backend"], inp["port"], rt_priority=50)
        self.discover = discover
        self.discovery_targets = discovery_targets or ["255.255.255.255"]
        self.transport = None
        self.sender = None
        self.last_seq = 0
        self.last_frame = None       # monotonic time of the newest frame
        self.loss_applied = False
        self.dmx_enabled = True      # off: frames are still received and counted, but drive nothing
        self.ip_changes = []         # (time, name, old ip, new ip), for the UI
        self.info = {}               # mac -> model, hw_ver, fw, rssi, alias, power_on (not saved)
        from engine.board import Board
        self.board = Board(self.patched_fixtures)
        self._last_discovery = -REDISCOVER_EVERY_S
        self._wake = asyncio.Event()
        self._stopping = False

    # ---- lifecycle ---------------------------------------------------------

    async def start(self):
        self.transport = await kasa.KasaTransport.create(port=self.cfg["kasa_port"])
        self.sender = Sender(self.cfg, self.transport.send)
        self.transport.on_reply = self._on_reply
        self.receiver.start()
        asyncio.get_running_loop().add_reader(self.receiver.wake_fd, self._on_wake)
        log.info("engine started: %d bulbs, input %s", len(self.cfg["bulbs"]), self.receiver.backend)

    def _on_reply(self, ip, reply, t):
        self.sender.on_reply(ip, reply, t)
        power = kasa.power_from_reply(reply)
        if power is not None:
            rt = self.sender.by_ip.get(ip)
            if rt is not None:
                rec = self.info.setdefault(rt.mac, {})
                rec["power_mw"], rec["lumens"] = power
                rec["power_t"] = time.time()

    def _on_wake(self):
        self.receiver.drain()
        self._wake.set()

    async def run(self):
        await self.start()
        housekeeping = asyncio.create_task(self._housekeeping())
        try:
            while not self._stopping:
                try:
                    await asyncio.wait_for(self._wake.wait(), TICK_S)
                except asyncio.TimeoutError:
                    pass
                self._wake.clear()
                self.step(time.monotonic())
        finally:
            housekeeping.cancel()
            self.stop()

    def patched_fixtures(self):
        """[(label, start channel, channels)] for every patched address, in channel
        order: solo bulbs, and group channels (labeled with the group name)."""
        seen = {}
        for name, g in self.cfg["groups"].items():
            if g["channel"] is not None:
                seen.setdefault(g["channel"], (f"Group {name}", group_size(self.cfg, name)))
        for b in self.cfg["bulbs"].values():
            if b["channel"] is not None and not b["follow"]:
                seen.setdefault(b["channel"], (b["name"] or "bulb", FOOTPRINT[b["mode"]]))
        return sorted(((label, ch, size) for ch, (label, size) in seen.items()), key=lambda x: x[1])

    def group_fixtures(self, name):
        """Start channels of a group's fixtures: its shared channel, plus members on
        their own (Solo) channels."""
        g = self.cfg["groups"][name]
        chans = {g["channel"]} if g["channel"] is not None else set()
        for b in self.cfg["bulbs"].values():
            if name in b["groups"] and b["channel"] is not None and not b["follow"]:
                chans.add(b["channel"])
        return sorted(chans)

    def stop(self):
        self._stopping = True
        try:
            self.board.release()
        except Exception:  # noqa: BLE001 - shutting down
            pass
        try:
            asyncio.get_running_loop().remove_reader(self.receiver.wake_fd)
        except (RuntimeError, ValueError):
            pass
        self.receiver.stop()
        if self.transport:
            self.transport.close()

    # ---- the main step ---------------------------------------------------------

    def step(self, now):
        seq, t, data = self.receiver.snapshot()
        if seq != self.last_seq:
            self.last_seq = seq
            self.last_frame = t
            if self.loss_applied:
                log.info("DMX is back")
                self.loss_applied = False
            if self.dmx_enabled:
                self.sender.update_dmx(data, t, now)
        if self.dmx_enabled:
            self._check_loss(now)
        self.sender.tick(now)

    def dmx_present(self, now):
        return self.last_frame is not None and now - self.last_frame <= LOSS_DETECT_S

    def _check_loss(self, now):
        if self.last_frame is None or self.loss_applied:
            return
        silent = now - self.last_frame
        loss = self.cfg["dmx_loss"]
        if silent > max(LOSS_DETECT_S, loss["after_s"]):
            self.loss_applied = True
            log.warning("no DMX for %.1f s; loss behavior: %s", silent, loss["mode"])
            if loss["mode"] != "hold":
                self.sender.dmx_lost(loss["mode"], now, self.cfg["looks"].get(loss["look"]))

    # ---- background work ---------------------------------------------------------

    async def _housekeeping(self):
        # Bulbs are identified by MAC; their stored IPs are only the last ones
        # seen. Check them straight away so a bulb that got a new DHCP lease
        # while we were off is found at once, not after it times out.
        try:
            self._last_discovery = time.monotonic()
            await self.rediscover()
            await self.read_power_on()
        except OSError as e:
            log.warning("start-up discovery failed: %s", e)
        last_info = time.monotonic()
        n = 0
        while True:
            await asyncio.sleep(1.0)
            n += 1
            self.receiver.check()
            now = time.monotonic()
            self._poll_quiet_bulbs(now)
            offline = [rt for rt in self.sender.bulbs.values()
                       if rt.ip and rt.sends and not rt.online(now, self.sender.offline_after)
                       and (rt.last_reply is None or now - rt.last_reply > REDISCOVER_AFTER_S)]
            if offline and now - self._last_discovery >= REDISCOVER_EVERY_S:
                self._last_discovery = now
                await self.rediscover()
            elif now - last_info >= INFO_EVERY_S:
                last_info = now
                self._last_discovery = now
                try:
                    await self.rediscover()
                    await self.read_power_on()
                except OSError as e:
                    log.warning("info refresh failed: %s", e)
            if n % 60 == 0:
                st = self.status(now)
                log.info("frames %d, sent %d, online %d/%d, latency p95 %s ms",
                         st["input"]["frames"], st["sender"]["sent"], st["online"], len(self.cfg["bulbs"]),
                         st["sender"]["latency_p95_ms"])

    def _poll_quiet_bulbs(self, now):
        """Ask bulbs that haven't been heard from lately for their light state,
        so online/offline is right even when nothing is being sent. The query
        doesn't change the bulb; its reply updates last_reply like any other."""
        period = self.cfg["sender"]["idle_check_s"]
        for rt in self.sender.bulbs.values():
            if not rt.ip:
                continue
            quiet = rt.last_reply is None or now - rt.last_reply > period
            if quiet and now - rt.last_send > period and now - rt.last_poll > period:
                rt.last_poll = now
                self.transport.send(rt.ip, kasa.IDLE_CHECK)

    async def rediscover(self):
        """Find bulbs by MAC and update any whose IP changed. Saves the config."""
        # Broadcast, plus each known bulb directly at its last IP (some Kasa
        # firmware has been reported to stop answering broadcast discovery).
        known = [b["ip"] for b in self.cfg["bulbs"].values() if b["ip"]]
        targets = list(dict.fromkeys(self.discovery_targets + known))
        found = await self.discover(targets, port=self.cfg["kasa_port"], timeout=2.0)
        changed = False
        for ip, info in found.items():
            mac = kasa.sysinfo_mac(info)
            if mac:
                self._note_info(mac, info)
            b = self.cfg["bulbs"].get(mac)
            if b is None or b["ip"] == ip:
                continue
            log.warning("bulb %s (%s) moved from %s to %s", b["name"] or mac, mac, b["ip"], ip)
            self.ip_changes.append((time.time(), b["name"] or mac, b["ip"], ip))
            # Another bulb may hold that IP in the config now (address swap).
            for other in self.cfg["bulbs"].values():
                if other is not b and other["ip"] == ip:
                    other["ip"] = None
            b["ip"] = ip
            changed = True
        if changed:
            self.cfg = self.store.save(self.cfg)
            self.sender.apply_config(self.cfg)
        return found

    def _note_info(self, mac, sysinfo):
        rec = self.info.setdefault(mac, {})
        rec.update(model=sysinfo.get("model"), hw_ver=sysinfo.get("hw_ver"), fw=sysinfo.get("sw_ver"),
                   rssi=sysinfo.get("rssi"), alias=sysinfo.get("alias"), seen=time.time())

    async def read_power_on(self, macs=None):
        """Read each bulb's power-on default (get_default_behavior) into self.info."""
        async def one(mac, ip):
            r = await self.transport.request(ip, kasa.INFO_CHECK, timeout=1.0)
            power = kasa.power_from_reply(r) if r else None
            if power is not None:
                rec = self.info.setdefault(mac, {})
                rec["power_mw"], rec["lumens"] = power
                rec["power_t"] = time.time()
            po = kasa.power_on_from_reply(r) if r else None
            if po is not None:
                self.info.setdefault(mac, {})["power_on"] = po
        jobs = [one(mac, rt.ip) for mac, rt in self.sender.bulbs.items()
                if rt.ip and (macs is None or mac in macs)]
        # A handful at a time, so 50 bulbs don't all answer at once.
        for i in range(0, len(jobs), 8):
            await asyncio.gather(*jobs[i:i + 8])

    # ---- operations for the web UI ---------------------------------------------

    def update_config(self, change):
        """Apply change(cfg_copy) -> None, validate, save and apply. Raises ConfigError."""
        import copy
        new = copy.deepcopy(self.cfg)
        change(new)
        self.cfg = self.store.save(new)
        self.sender.apply_config(self.cfg)
        return self.cfg

    def set_dmx_enabled(self, enabled):
        """Let DMX drive the bulbs, or ignore it (bulbs keep their current state).
        Not saved: the engine always starts with DMX enabled."""
        enabled = bool(enabled)
        if enabled == self.dmx_enabled:
            return
        self.dmx_enabled = enabled
        log.warning("DMX input %s from the web UI", "enabled" if enabled else "IGNORED")
        if enabled:
            # Re-apply the current frame so DMX-following bulbs catch up at once.
            seq, t, data = self.receiver.snapshot()
            if seq:
                self.sender.update_dmx(data, t, time.monotonic())

    def restart_receiver(self):
        """Switch to the configured input (after a settings change)."""
        loop = asyncio.get_running_loop()
        try:
            loop.remove_reader(self.receiver.wake_fd)
        except ValueError:
            pass
        self.receiver.stop()
        inp = self.cfg["input"]
        self.receiver = Receiver(inp["backend"], inp["port"], rt_priority=50)
        self.last_seq = 0
        self.receiver.start()
        loop.add_reader(self.receiver.wake_fd, self._on_wake)

    def set_color(self, macs, state):
        now = time.monotonic()
        for mac in macs:
            if mac in self.sender.bulbs:
                self.sender.set_manual(mac, state, now)

    def recall_look(self, name):
        self.sender.apply_look(self.cfg["looks"][name], time.monotonic())

    async def identify(self, mac):
        """Blink a bulb (white, full/dim) at the configured rate and duration,
        then put it back."""
        ident = self.cfg["identify"]
        half = 0.5 / ident["blink_hz"]
        rt = self.sender.bulbs[mac]
        before_target, before_source, before_raw = rt.target, rt.source, rt.manual_raw
        end = time.monotonic() + ident["duration_s"]
        self.sender.hold(mac, end + 0.5)
        on = True
        while time.monotonic() < end:
            if rt.ip:
                self.transport.send(rt.ip, kasa.temperature_state(5000, 100 if on else 1, 0))
            on = not on
            await asyncio.sleep(half)
        rt.target, rt.source, rt.manual_raw = before_target, before_source, before_raw
        rt.sent = None              # force the restore to be sent
        rt.last_send = -1e9

    async def find_bulbs(self):
        """Discovery for the UI: every bulb found, marked new or known."""
        found = await self.discover(self.discovery_targets, port=self.cfg["kasa_port"], timeout=2.0)
        out = []
        for ip, info in sorted(found.items()):
            mac = kasa.sysinfo_mac(info)
            out.append({"ip": ip, "mac": mac, "alias": info.get("alias"), "model": info.get("model"),
                        "hw_ver": info.get("hw_ver"), "fw": info.get("sw_ver"), "rssi": info.get("rssi"),
                        "known": mac in self.cfg["bulbs"]})
        return out

    async def set_power_on(self, macs, state):
        """Set each bulb's power-on default; returns {mac: ok}. state is
        ("temp", k, v), ("hsv", h, s, v) or ("last",) for "come back as it was"."""
        if state[0] == "last":
            cmd = kasa.LAST_STATE_ON
        elif state[0] == "temp":
            cmd = kasa.preferred_state(0, 0, state[2], state[1])
        else:
            cmd = kasa.preferred_state(state[1], state[2], state[3], 0)
        results = {}
        for mac in macs:
            rt = self.sender.bulbs.get(mac)
            if not rt or not rt.ip:
                results[mac] = False
                continue
            r = await self.transport.request(rt.ip, cmd, timeout=1.0)
            svc = (r or {}).get(kasa.LIGHTING, {})
            res = svc.get("set_default_behavior" if state[0] == "last" else "set_preferred_state", {})
            results[mac] = res.get("err_code") == 0
        await self.read_power_on([m for m, ok in results.items() if ok])
        return results

    async def rename_device(self, mac, name):
        """Store the name on the bulb too. Returns "renamed", "offline" or an error.
        The bulb's next reply may be a light-state echo rather than ours, so try a
        few times."""
        rt = self.sender.bulbs.get(mac)
        if not rt or not rt.ip:
            return "offline"
        got_reply = False
        for _ in range(3):
            r = await self.transport.request(rt.ip, kasa.set_alias(name), timeout=1.0)
            if r is None:
                continue
            got_reply = True
            res = r.get("system", {}).get("set_dev_alias")
            if res is None:
                continue                      # someone else's reply
            if res.get("err_code") == 0:
                self.info.setdefault(mac, {})["alias"] = name[:kasa.ALIAS_MAX]
                return "renamed"
            return res.get("err_msg") or f"error {res.get('err_code')}"
        return "no answer" if got_reply else "offline"

    async def bulb_info(self, mac):
        rt = self.sender.bulbs.get(mac)
        if not rt or not rt.ip:
            return None
        r = await self.transport.request(rt.ip, kasa.SYSINFO, timeout=1.0)
        return r and r["system"]["get_sysinfo"]

    async def update_firmware(self, mac, image_url, progress=None):
        """Tell one bulb to download (and, on KL bulbs, flash) the image at
        image_url, served by this Pi. Returns the bulb's new version or raises."""
        rt = self.sender.bulbs[mac]
        ns = "smartlife.iot.common.system"
        r = await self.transport.request(rt.ip, {ns: {"download_firmware": {"url": image_url}}}, timeout=3.0)
        if not r or r.get(ns, {}).get("download_firmware", {}).get("err_code") != 0:
            raise RuntimeError(f"the bulb refused the download: {r}")
        start = time.monotonic()
        while time.monotonic() - start < 180:
            await asyncio.sleep(2)
            st = await self.transport.request(rt.ip, {ns: {"get_download_state": {}}}, timeout=2.0)
            s = (st or {}).get(ns, {}).get("get_download_state")
            if s and progress:
                progress(s.get("ratio", 0), s.get("status"))
            if s and s.get("err_code", 0) != 0:
                raise RuntimeError(f"download failed: {s}")
            if s and s.get("status") == 2:
                break
        await asyncio.sleep(15)     # flash + reboot
        for _ in range(30):
            found = await self.discover(self.discovery_targets, port=self.cfg["kasa_port"], timeout=2.0)
            for ip, info in found.items():
                if kasa.sysinfo_mac(info) == mac:
                    self._note_info(mac, info)
                    if ip != rt.ip:
                        self.update_config(lambda c: c["bulbs"][mac].update(ip=ip))
                    return info.get("sw_ver")
            await asyncio.sleep(2)
        raise RuntimeError("the bulb didn't come back after flashing; it may still be rebooting")

    # ---- reporting ---------------------------------------------------------

    def status(self, now=None):
        now = now or time.monotonic()
        s = self.sender
        bulbs = s.bulb_status(now)
        ms = lambda v: None if v is None else round(v * 1000, 1)  # noqa: E731
        return {
            "dmx_present": self.dmx_present(now),
            "dmx_enabled": self.dmx_enabled,
            "last_frame_age_s": None if self.last_frame is None else round(now - self.last_frame, 1),
            "dmx_lost_applied": self.loss_applied,
            "input": self.receiver.stats(),
            "sender": {**s.stats,
                       "latency_p50_ms": ms(percentile(s.latency, 0.5)),
                       "latency_p95_ms": ms(percentile(s.latency, 0.95)),
                       "latency_max_ms": ms(max(s.latency)) if s.latency else None,
                       "latency_hist": {"edges_ms": HIST_EDGES_MS, "counts": histogram(s.change_latency)},
                       "queued_p95_ms": ms(percentile(s.queued, 0.95)),
                       "mode": s.mode,
                       "frame_period_ms": ms(s.frame_period) if s.mode == "sync" else None,
                       "delivery_spread_p50_ms": ms(percentile(s.delivery_spread, 0.5)),
                       "delivery_spread_p95_ms": ms(percentile(s.delivery_spread, 0.95)),
                       "reply_spread_p50_ms": ms(percentile(s.reply_spread, 0.5)),
                       "reply_spread_p95_ms": ms(percentile(s.reply_spread, 0.95)),
                       "transport_errors": self.transport.send_errors if self.transport else 0},
            "online": sum(1 for b in bulbs.values() if b["online"]),
            "bulbs": bulbs,
            "ip_changes": self.ip_changes[-20:],
            "info": self.info,
        }
