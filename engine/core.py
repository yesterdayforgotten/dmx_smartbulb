"""The engine: receiver -> sender -> bulbs, plus DMX-loss handling and finding
bulbs again by MAC when their IP changes.
"""

import asyncio
import logging
import time

from engine import kasa
from engine.receiver import Receiver
from engine.sender import Sender, percentile

log = logging.getLogger("engine")

LOSS_DETECT_S = 1.0          # no frame for this long means DMX is lost
TICK_S = 0.01                # tick at least this often without new frames
REDISCOVER_AFTER_S = 10.0    # a bulb offline this long triggers rediscovery
REDISCOVER_EVERY_S = 30.0    # at most this often
POLL_QUIET_S = 3.0           # status-query bulbs not heard from for this long


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
        self.ip_changes = []         # (time, name, old ip, new ip), for the UI
        self._last_discovery = -REDISCOVER_EVERY_S
        self._wake = asyncio.Event()
        self._stopping = False

    # ---- lifecycle ---------------------------------------------------------

    async def start(self):
        self.transport = await kasa.KasaTransport.create(port=self.cfg["kasa_port"])
        self.sender = Sender(self.cfg, self.transport.send)
        self.transport.on_reply = self.sender.on_reply
        self.receiver.start()
        asyncio.get_running_loop().add_reader(self.receiver.wake_fd, self._on_wake)
        log.info("engine started: %d bulbs, input %s", len(self.cfg["bulbs"]), self.receiver.backend)

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

    def stop(self):
        self._stopping = True
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
            self.sender.update_dmx(data, t, now)
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
            log.warning("no DMX for %.1f s; loss behaviour: %s", silent, loss["mode"])
            if loss["mode"] != "hold":
                self.sender.dmx_lost(loss["mode"], now, self.cfg["looks"].get(loss["look"]))

    # ---- background work ---------------------------------------------------------

    async def _housekeeping(self):
        n = 0
        while True:
            await asyncio.sleep(1.0)
            n += 1
            self.receiver.check()
            now = time.monotonic()
            self._poll_quiet_bulbs(now)
            offline = [rt for rt in self.sender.bulbs.values()
                       if rt.ip and rt.sends and not rt.online(now)
                       and (rt.last_reply is None or now - rt.last_reply > REDISCOVER_AFTER_S)]
            if offline and now - self._last_discovery >= REDISCOVER_EVERY_S:
                self._last_discovery = now
                await self.rediscover()
            if n % 60 == 0:
                st = self.status(now)
                log.info("frames %d, sent %d, online %d/%d, latency p95 %s ms",
                         st["input"]["frames"], st["sender"]["sent"], st["online"], len(self.cfg["bulbs"]),
                         st["sender"]["latency_p95_ms"])

    def _poll_quiet_bulbs(self, now):
        """Ask bulbs that haven't been heard from lately for their status, so
        online/offline is right even when nothing is being sent. A status query
        doesn't change the bulb; its reply updates last_reply like any other."""
        for rt in self.sender.bulbs.values():
            if not rt.ip:
                continue
            quiet = rt.last_reply is None or now - rt.last_reply > POLL_QUIET_S
            if quiet and now - rt.last_send > POLL_QUIET_S and now - rt.last_poll > POLL_QUIET_S:
                rt.last_poll = now
                self.transport.send(rt.ip, kasa.SYSINFO)

    async def rediscover(self):
        """Find bulbs by MAC and update any whose IP changed. Saves the config."""
        found = await self.discover(self.discovery_targets, port=self.cfg["kasa_port"], timeout=2.0)
        changed = False
        for ip, info in found.items():
            mac = kasa.sysinfo_mac(info)
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

    # ---- operations for the web UI ---------------------------------------------

    def update_config(self, change):
        """Apply change(cfg_copy) -> None, validate, save and apply. Raises ConfigError."""
        import copy
        new = copy.deepcopy(self.cfg)
        change(new)
        self.cfg = self.store.save(new)
        self.sender.apply_config(self.cfg)
        return self.cfg

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

    async def identify(self, mac, seconds=4.0):
        """Blink a bulb (white, full/off at 2 Hz), then put it back."""
        rt = self.sender.bulbs[mac]
        before_target, before_source, before_raw = rt.target, rt.source, rt.manual_raw
        end = time.monotonic() + seconds
        self.sender.hold(mac, end + 0.5)
        on = True
        while time.monotonic() < end:
            if rt.ip:
                self.transport.send(rt.ip, kasa.temperature_state(5000, 100 if on else 1, 0))
            on = not on
            await asyncio.sleep(0.25)
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
        """Set each bulb's power-on default; returns {mac: ok}."""
        if state[0] == "temp":
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
            res = (r or {}).get(kasa.LIGHTING, {}).get("set_preferred_state", {})
            results[mac] = res.get("err_code") == 0
        return results

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
            "dmx_lost_applied": self.loss_applied,
            "input": self.receiver.stats(),
            "sender": {**s.stats,
                       "latency_p50_ms": ms(percentile(s.latency, 0.5)),
                       "latency_p95_ms": ms(percentile(s.latency, 0.95)),
                       "queued_p95_ms": ms(percentile(s.queued, 0.95)),
                       "transport_errors": self.transport.send_errors if self.transport else 0},
            "online": sum(1 for b in bulbs.values() if b["online"]),
            "bulbs": bulbs,
            "ip_changes": self.ip_changes[-20:],
        }
