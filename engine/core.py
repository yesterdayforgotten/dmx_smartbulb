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
