"""Turns DMX frames and manual commands into bulb commands, within a send budget.

Each tick (a new DMX frame, or every 10 ms):

1. Every bulb that follows DMX works out its target colour from its own three
   channels (hue, saturation, brightness), with the brightness curve applied.
   Change tracking is per bulb, so bulbs sharing channels all update together.
2. "DMX wins": a manual set or a recalled look holds until that bulb's DMX
   values change from what they were at the moment of the manual set. A bulb
   with its DMX toggle off ignores DMX entirely.
3. Bulbs with a pending change and whose interval has passed are sent in order
   of largest change first (plus a bonus for time waited, so nothing starves),
   while the global packets-per-second budget lasts.
   Leftover budget refreshes bulbs whose last command is older than refresh_s.
4. Replies (KL bulbs answer every command) mark a bulb online and measure its
   round-trip time. A command with no reply within REPLY_TIMEOUT counts as a
   miss and doubles that bulb's interval, up to max_backoff_ms; replies ease
   it back toward min_interval_ms.

This module does no I/O itself: send(ip, command) is injected and time is passed
in, so the logic can be tested exactly.
"""

import collections
import math

from engine import kasa
from engine.config import bulb_channel

REPLY_TIMEOUT = 0.5      # s: a command not answered within this is a miss
OFFLINE_AFTER = 5.0      # s without any reply while commands are going out
BURST_S = 0.05           # the budget may be spent this far ahead (token bucket depth)
AGE_WEIGHT = 10.0        # priority added per second a change has waited (100 ms ~ a full change)


def apply_curve(raw, curve):
    """Brightness byte (0-255) through the curve, to Kasa brightness 0-100.
    Raw 0-2 is off for every curve; anything above stays at least 1%."""
    if raw <= 2:
        return 0
    x = raw / 255
    if curve == "square":
        x = x * x
    elif curve == "scurve":
        x = x * x * (3 - 2 * x)
    return max(1, int(x * 100))


def dmx_to_state(raw3, curve):
    h, s, _ = kasa.scale_hsv(*raw3)
    return ("hsv", h, s, apply_curve(raw3[2], curve))


def change_size(a, b):
    """How different two states look, from 0 (same) to 1 (completely)."""
    if a is None or b is None or a[0] != b[0]:
        return 1.0
    if a[0] == "hsv":
        dh = abs(a[1] - b[1]) % 360
        dh = min(dh, 360 - dh) / 180
        # Hue and saturation matter less when the bulb is dim.
        weight = max(a[3], b[3]) / 100
        return max(dh * weight, abs(a[2] - b[2]) / 100 * weight, abs(a[3] - b[3]) / 100)
    return max(abs(a[1] - b[1]) / 4000, abs(a[2] - b[2]) / 100)


def command_for(state, transition_ms):
    if state[0] == "hsv":
        return kasa.light_state(state[1], state[2], state[3], transition_ms)
    return kasa.temperature_state(state[1], state[2], transition_ms)


class BulbRuntime:
    def __init__(self, mac):
        self.mac = mac
        self.ip = None
        self.channel = None
        self.dmx = True
        self.name = ""
        self.source = "dmx"          # dmx | manual | look | loss
        self.target = None           # the state the bulb should be in
        self.sent = None             # the state last sent
        self.raw = None              # this bulb's last DMX triple
        self.manual_raw = None       # DMX triple when a manual set was made
        self.dirty_since = None      # when target last changed away from sent
        self.dmx_time = None         # frame time of the DMX change behind target
        self.last_send = -math.inf
        self.interval = 0.05
        self.pending = collections.deque()   # send times not yet answered
        self.last_reply = None
        self.replies = self.misses = self.sends = 0
        self.rtt = None              # smoothed round-trip time, s
        self.hold_until = 0.0        # ignore DMX until then (identify)

    @property
    def dirty(self):
        return self.target is not None and self.target != self.sent

    def online(self, now):
        if self.last_reply is None:
            return False
        return now - self.last_reply < OFFLINE_AFTER

    def backed_off(self, min_interval):
        return self.interval > min_interval * 1.01


class Sender:
    def __init__(self, cfg, send):
        self.send = send
        self.bulbs = {}
        self.by_ip = {}
        self.tokens = 0.0
        self.last_tick = None
        self.stats = {"sent": 0, "refreshes": 0, "budget_waits": 0}
        self.latency = collections.deque(maxlen=500)   # DMX frame -> packet sent, s
        self.queued = collections.deque(maxlen=500)    # change -> packet sent, s
        self.apply_config(cfg)

    # ---- configuration -------------------------------------------------

    def apply_config(self, cfg):
        self.cfg = cfg
        s = cfg["sender"]
        self.min_interval = s["min_interval_ms"] / 1000
        self.max_interval = s["max_backoff_ms"] / 1000
        self.budget = s["budget_pps"]
        self.refresh_s = s["refresh_s"]
        self.adaptive = s["adaptive_transition"]
        self.fixed_ms = s["fixed_transition_ms"]
        self.snap = s["snap_threshold"]
        self.curve = s["curve"]
        old = self.bulbs
        self.bulbs = {}
        for mac, b in cfg["bulbs"].items():
            rt = old.get(mac) or BulbRuntime(mac)
            rt.ip, rt.name, rt.dmx = b["ip"], b["name"], b["dmx"]
            rt.channel = bulb_channel(cfg, mac)
            rt.interval = min(max(rt.interval, self.min_interval), self.max_interval)
            self.bulbs[mac] = rt
        self.by_ip = {rt.ip: rt for rt in self.bulbs.values() if rt.ip}

    def set_ip(self, mac, ip):
        rt = self.bulbs[mac]
        self.by_ip.pop(rt.ip, None)
        rt.ip = ip
        self.by_ip[ip] = rt

    # ---- inputs ---------------------------------------------------------

    def _set_target(self, rt, state, now, source, dmx_time=None):
        rt.source = source
        if state != rt.target:
            rt.target = state
            if state == rt.sent:
                rt.dirty_since = rt.dmx_time = None
            elif rt.dirty_since is None:
                # Keep the time of the *first* pending change, so a bulb that
                # changes every frame still builds up waiting priority.
                rt.dirty_since = now
                rt.dmx_time = dmx_time

    def update_dmx(self, data, frame_time, now):
        """Apply a DMX frame (512 bytes)."""
        for rt in self.bulbs.values():
            if not rt.dmx or rt.channel is None:
                continue
            c = rt.channel - 1
            raw = (data[c], data[c + 1], data[c + 2])
            rt.raw = raw
            if now < rt.hold_until:
                continue            # identify is running
            if rt.source in ("manual", "look"):
                if raw == rt.manual_raw:
                    continue        # DMX hasn't moved since the manual set; keep it
            self._set_target(rt, dmx_to_state(raw, self.curve), now, "dmx", frame_time)

    def set_manual(self, mac, state, now, source="manual"):
        """state: ("hsv", h, s, v) or ("temp", kelvin, v). Holds until DMX changes."""
        rt = self.bulbs[mac]
        rt.manual_raw = rt.raw
        self._set_target(rt, state, now, source)

    def hold(self, mac, until):
        """Ignore DMX for this bulb until `until` (used while identifying)."""
        self.bulbs[mac].hold_until = until

    def snapshot_states(self, macs=None):
        """{mac: look entry} of the bulbs' current targets, for saving a look."""
        out = {}
        for mac, rt in self.bulbs.items():
            if (macs is None or mac in macs) and rt.target is not None:
                st = rt.target
                out[mac] = {"k": st[1], "v": st[2]} if st[0] == "temp" else {"h": st[1], "s": st[2], "v": st[3]}
        return out

    def apply_look(self, look, now):
        for mac, st in look.items():
            if mac in self.bulbs:
                state = ("temp", st["k"], st["v"]) if "k" in st else ("hsv", st["h"], st["s"], st["v"])
                self.set_manual(mac, state, now, source="look")

    def dmx_lost(self, mode, now, look=None):
        """DMX has been missing long enough: blackout or recall a look for the
        bulbs that follow DMX. ('hold' needs no action.)"""
        for rt in self.bulbs.values():
            if not rt.dmx or rt.channel is None or rt.source in ("manual", "look"):
                continue
            if mode == "blackout":
                self._set_target(rt, ("hsv", rt.target[1] if rt.target and rt.target[0] == "hsv" else 0, 0, 0),
                                 now, "loss")
            elif mode == "look" and look and rt.mac in look:
                st = look[rt.mac]
                state = ("temp", st["k"], st["v"]) if "k" in st else ("hsv", st["h"], st["s"], st["v"])
                self._set_target(rt, state, now, "loss")

    # ---- replies ---------------------------------------------------------

    def on_reply(self, ip, reply, t):
        rt = self.by_ip.get(ip)
        if rt is None:
            return
        rt.last_reply = t
        rt.replies += 1
        if rt.pending:
            sample = t - rt.pending.popleft()
            rt.rtt = sample if rt.rtt is None else rt.rtt * 0.8 + sample * 0.2
        # Recovering: ease the interval back toward the configured minimum.
        if rt.interval > self.min_interval:
            rt.interval = max(self.min_interval, rt.interval * 0.9)

    def _expire(self, rt, now):
        while rt.pending and now - rt.pending[0] > REPLY_TIMEOUT:
            rt.pending.popleft()
            rt.misses += 1
            rt.interval = min(self.max_interval, max(rt.interval, self.min_interval) * 2)

    # ---- sending ---------------------------------------------------------

    def _transition_ms(self, rt, size):
        if not self.adaptive:
            return self.fixed_ms
        if size >= self.snap:
            return 0
        return int(rt.interval * 1000)

    def _send(self, rt, now, refresh=False):
        size = change_size(rt.sent, rt.target)
        trans = 0 if refresh and rt.target == rt.sent else self._transition_ms(rt, size)
        if not self.send(rt.ip, command_for(rt.target, trans)):
            return False
        if not refresh or rt.target != rt.sent:
            if rt.dmx_time is not None:
                self.latency.append(now - rt.dmx_time)
                rt.dmx_time = None
            if rt.dirty_since is not None:
                self.queued.append(now - rt.dirty_since)
        rt.sent = rt.target
        rt.dirty_since = None
        rt.last_send = now
        rt.sends += 1
        rt.pending.append(now)
        if len(rt.pending) > 20:
            rt.pending.popleft()
        self.stats["sent"] += 1
        if refresh:
            self.stats["refreshes"] += 1
        return True

    def tick(self, now):
        """Send what the budget allows. Returns the number of packets sent."""
        if self.last_tick is not None:
            self.tokens = min(self.tokens + (now - self.last_tick) * self.budget,
                              max(1.0, self.budget * BURST_S))
        else:
            self.tokens = max(1.0, self.budget * BURST_S)
        self.last_tick = now

        for rt in self.bulbs.values():
            self._expire(rt, now)

        ready = [rt for rt in self.bulbs.values()
                 if rt.ip and rt.dirty and now - rt.last_send >= rt.interval]
        # Largest change first; waiting adds priority so small changes can't starve.
        ready.sort(key=lambda rt: change_size(rt.sent, rt.target)
                   + AGE_WEIGHT * (now - (rt.dirty_since if rt.dirty_since is not None else now)),
                   reverse=True)
        sent = 0
        for rt in ready:
            if self.tokens < 1:
                self.stats["budget_waits"] += 1
                break
            if self._send(rt, now):
                self.tokens -= 1
                sent += 1

        if self.tokens >= 1:
            stale = [rt for rt in self.bulbs.values()
                     if rt.ip and rt.target is not None and not rt.dirty
                     and now - rt.last_send >= max(self.refresh_s, rt.interval)]
            stale.sort(key=lambda rt: rt.last_send)
            for rt in stale:
                if self.tokens < 1:
                    break
                if self._send(rt, now, refresh=True):
                    self.tokens -= 1
                    sent += 1
        return sent

    # ---- reporting ---------------------------------------------------------

    def bulb_status(self, now):
        out = {}
        for mac, rt in self.bulbs.items():
            out[mac] = {
                "name": rt.name, "ip": rt.ip, "channel": rt.channel, "dmx": rt.dmx,
                "source": rt.source, "state": rt.target, "online": rt.online(now),
                "rtt_ms": None if rt.rtt is None else round(rt.rtt * 1000, 1),
                "reply_rate": round(rt.replies / rt.sends, 3) if rt.sends else None,
                "interval_ms": round(rt.interval * 1000), "backoff": rt.backed_off(self.min_interval),
            }
        return out


def percentile(values, p):
    if not values:
        return None
    v = sorted(values)
    return v[min(len(v) - 1, int(len(v) * p))]
