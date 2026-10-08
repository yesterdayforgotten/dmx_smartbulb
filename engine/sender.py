"""Turns DMX frames and manual commands into bulb commands, within a send budget.

Each tick (a new DMX frame, or every 10 ms):

1. Every bulb that follows DMX works out its target color from its own three
   channels (hue, saturation, brightness), with the brightness curve applied.
   An HSIC bulb reads a fourth, color temperature: at saturation 0 it switches
   to its white LEDs at that temperature instead of mixing white from color.
   Change tracking is per bulb, so bulbs sharing channels all update together.
2. "DMX wins": a manual set or a recalled look holds until that bulb's DMX
   values change from what they were at the moment of the manual set. A bulb
   with its DMX toggle off ignores DMX entirely.
3. Bulbs with a pending change and whose interval has passed are sent in order
   of largest change first (plus a bonus for time waited, so nothing starves),
   while the global packets-per-second budget lasts.
   Leftover budget re-sends to bulbs whose last command wasn't confirmed by a
   reply within refresh_s (a confirmed bulb is known to be showing it).
4. Replies (KL bulbs answer every command) mark a bulb online and measure its
   round-trip time. A command with no reply within REPLY_TIMEOUT counts as a
   miss; backoff_after misses in a row (a setting) double that bulb's interval (up to
   max_backoff_ms), so the odd lost WiFi packet doesn't slow a bulb down.
   Replies ease it back toward min_interval_ms.

Send modes (sender.mode):
    priority  the token bucket above: largest change first, as budget allows.
    sync      fixed output frames of period P = max(min interval, active bulbs /
              budget). Each frame sends to every changed bulb at once with the
              same transition (P), so bulbs move together instead of trickling.

Sync is measured two ways: delivery spread (for a DMX frame that changes 2+
bulbs, first to last bulb sent that change) and reply spread (first to last
reply within one burst of commands: network and bulb timing).

This module does no I/O itself: send(ip, command) is injected and time is passed
in, so the logic can be tested exactly.
"""

import collections
import math

from engine import kasa
from engine.config import FOOTPRINT, bulb_channel

REPLY_TIMEOUT = 0.5      # s: a command not answered within this is a miss
OFFLINE_AFTER = 5.0      # s without any reply: offline (at least 2.5 idle checks, see apply_config)
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


WHITE_K = (2500, 6500)   # color temperature channel 0..255 covers this range


def cct_to_kelvin(byte):
    lo, hi = WHITE_K
    return round((lo + byte / 255 * (hi - lo)) / 10) * 10


def dmx_to_state(raw, curve):
    """raw: (hue, sat, bri) or, for HSIC, (hue, sat, bri, color temperature)."""
    h, s, _ = kasa.scale_hsv(*raw[:3])
    v = apply_curve(raw[2], curve)
    if len(raw) == 4 and s == 0:
        return ("temp", cct_to_kelvin(raw[3]), v)
    return ("hsv", h, s, v)


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


def state_key(state):
    """What a bulb echoes back for this state, to match replies to commands."""
    if state[0] == "hsv":
        return ("off",) if state[3] == 0 else ("hsv", 180 if state[2] == 0 else state[1], state[2], state[3])
    return ("off",) if state[2] == 0 else ("temp", round(state[1]), state[2])


def _light_key(st):
    if not st.get("on_off", 1):
        return ("off",)
    if st.get("color_temp"):
        return ("temp", st["color_temp"], st.get("brightness"))
    return ("hsv", st.get("hue"), st.get("saturation"), st.get("brightness"))


def polled_key(reply):
    """The state a get_light_state reply reports, as a state_key, or None."""
    try:
        return _light_key(reply[kasa.LIGHTING]["get_light_state"])
    except (KeyError, TypeError, AttributeError):
        return None


def reply_key(reply):
    """The state a light-state reply reports, as a state_key, or None."""
    try:
        st = reply[kasa.LIGHTING]["transition_light_state"]
    except (KeyError, TypeError):
        return None
    if not st.get("on_off", 1):
        return ("off",)
    if st.get("color_temp"):
        return ("temp", st["color_temp"], st.get("brightness"))
    return ("hsv", st.get("hue"), st.get("saturation"), st.get("brightness"))


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
        self.raw = None              # this bulb's last DMX values (3, or 4 in HSIC)
        self.manual_raw = None       # DMX values when a manual set was made
        self.size = 3                # channels it reads: 3 (HSI) or 4 (HSIC)
        self.dirty_since = None      # when target last changed away from sent
        self.dmx_time = None         # frame time of the DMX change behind target
        self.last_send = -math.inf
        self.interval = 0.05
        self.pending = collections.deque()   # send times not yet answered
        self.last_reply = None
        self.replies = self.misses = self.sends = 0
        self.miss_streak = 0         # misses since the last reply
        self.rtt = None              # smoothed round-trip time, s
        self.hold_until = 0.0        # ignore DMX until then (identify)
        self.last_poll = -math.inf   # last status query (engine polls quiet bulbs)
        self.target_frame = None     # DMX frame time behind the current target
        self.confirmed = False       # the bulb has echoed the state we last sent

    @property
    def dirty(self):
        return self.target is not None and self.target != self.sent

    def online(self, now, after=None):
        if self.last_reply is None:
            return False
        return now - self.last_reply < (after or OFFLINE_AFTER)

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
        # The same, once per DMX frame rather than per bulb: in sync mode every bulb a
        # frame changed goes out in one round with the same delay.
        self.change_latency = collections.deque(maxlen=500)
        self._last_change_frame = None
        self.queued = collections.deque(maxlen=500)    # change -> packet sent, s
        self.delivery_spread = collections.deque(maxlen=500)  # per DMX frame: first -> last bulb sent, s
        self.reply_spread = collections.deque(maxlen=500)     # per burst: first -> last reply, s
        self._frames = collections.deque()   # DMX frames that changed 2+ bulbs, awaiting delivery
        self._bursts = {}                    # burst id -> [sent time, [reply times]]
        self.burst_id = 0
        self.frame_next = 0.0
        self.frame_period = None
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
        self.mode = s["mode"]
        self.backoff_after = s["backoff_after"]
        # Idle bulbs only answer the periodic status check, so one lost reply
        # must not flag them offline: allow a couple of missed checks.
        self.offline_after = max(OFFLINE_AFTER, 2.5 * s["idle_check_s"])
        old = self.bulbs
        self.bulbs = {}
        for mac, b in cfg["bulbs"].items():
            rt = old.get(mac) or BulbRuntime(mac)
            rt.ip, rt.name, rt.dmx = b["ip"], b["name"], b["dmx"]
            rt.channel = bulb_channel(cfg, mac)
            rt.size = FOOTPRINT[b["mode"]]
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
        changed = []
        for rt in self.bulbs.values():
            if not rt.dmx or rt.channel is None:
                continue
            c = rt.channel - 1
            raw = tuple(data[c:c + 4]) if rt.size == 4 and c + 4 <= len(data) else tuple(data[c:c + 3])
            rt.raw = raw
            if now < rt.hold_until:
                continue            # identify is running
            if rt.source in ("manual", "look"):
                if raw == rt.manual_raw:
                    continue        # DMX hasn't moved since the manual set; keep it
            state = dmx_to_state(raw, self.curve)
            if state != rt.target:
                rt.target_frame = frame_time
                changed.append(rt.mac)
            self._set_target(rt, state, now, "dmx", frame_time)
        if len(changed) >= 2:
            self._frames.append({"t": frame_time, "pending": set(changed), "first": None, "last": None})

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
        key = reply_key(reply)
        if key is None:
            # A status query's reply: proves it's online. If the bulb isn't
            # showing what we last sent (e.g. it lost power and came back on
            # its power-on default), un-confirm it so the refresh restores it.
            polled = polled_key(reply)
            if polled is not None and rt.sent is not None and polled != state_key(rt.sent):
                rt.confirmed = False
            return
        rt.replies += 1
        # Match the reply to the command it answers. Commands sent before it
        # whose replies never came were lost.
        idx = next((i for i, (_, k, _b) in enumerate(rt.pending) if k == key), None)
        if idx is None:
            idx = 0 if rt.pending else None     # unrecognized echo: assume the oldest
        if idx is not None:
            lost = idx
            for _ in range(idx):
                rt.pending.popleft()
            sent_at, matched_key, burst = rt.pending.popleft()
            if rt.sent is not None and matched_key == state_key(rt.sent):
                rt.confirmed = True
            if burst in self._bursts:
                self._bursts[burst][1].append(t)
            sample = t - sent_at
            rt.rtt = sample if rt.rtt is None else rt.rtt * 0.8 + sample * 0.2
            rt.misses += lost
            if lost >= self.backoff_after:
                self._back_off(rt)
        rt.miss_streak = 0
        # Recovering: ease the interval back toward the configured minimum.
        if rt.interval > self.min_interval:
            rt.interval = max(self.min_interval, rt.interval * 0.9)

    def _back_off(self, rt):
        rt.interval = min(self.max_interval, max(rt.interval, self.min_interval) * 2)

    def _expire(self, rt, now):
        """Commands unanswered for REPLY_TIMEOUT: the bulb has gone quiet."""
        while rt.pending and now - rt.pending[0][0] > REPLY_TIMEOUT:
            rt.pending.popleft()
            rt.misses += 1
            rt.miss_streak += 1
            if rt.miss_streak >= self.backoff_after:
                self._back_off(rt)

    # ---- sending ---------------------------------------------------------

    def _transition_ms(self, rt, size):
        if not self.adaptive:
            return self.fixed_ms
        if size >= self.snap:
            return 0
        return int(rt.interval * 1000)

    def _send(self, rt, now, refresh=False, transition_ms=None):
        size = change_size(rt.sent, rt.target)
        if refresh and rt.target == rt.sent:
            trans = 0
        elif transition_ms is not None:
            trans = transition_ms if not self.adaptive or size < self.snap else 0
        else:
            trans = self._transition_ms(rt, size)
        if not self.send(rt.ip, command_for(rt.target, trans)):
            return False
        if not refresh or rt.target != rt.sent:
            if rt.dmx_time is not None:
                self.latency.append(now - rt.dmx_time)
                if rt.dmx_time != self._last_change_frame:
                    self._last_change_frame = rt.dmx_time
                    self.change_latency.append(now - rt.dmx_time)
                rt.dmx_time = None
            if rt.dirty_since is not None:
                self.queued.append(now - rt.dirty_since)
        if rt.target != rt.sent and rt.target_frame is not None:
            for fr in self._frames:
                if fr["t"] <= rt.target_frame and rt.mac in fr["pending"]:
                    fr["pending"].discard(rt.mac)
                    fr["first"] = now if fr["first"] is None else fr["first"]
                    fr["last"] = now
        rt.sent = rt.target
        rt.confirmed = False
        rt.dirty_since = None
        rt.last_send = now
        rt.sends += 1
        rt.pending.append((now, state_key(rt.target), self.burst_id))
        if len(rt.pending) > 20:
            rt.pending.popleft()
        self.stats["sent"] += 1
        if refresh:
            self.stats["refreshes"] += 1
        return True

    def _housekeep_metrics(self, now):
        while self._frames and (not self._frames[0]["pending"] or now - self._frames[0]["t"] > 3.0):
            fr = self._frames.popleft()
            if not fr["pending"] and fr["first"] is not None:
                self.delivery_spread.append(fr["last"] - fr["first"])
        for b in [b for b, (t0, _) in self._bursts.items() if now - t0 > 1.0]:
            _, replies = self._bursts.pop(b)
            if len(replies) >= 2:
                self.reply_spread.append(max(replies) - min(replies))

    def tick(self, now):
        """Send what the budget allows. Returns the number of packets sent."""
        self._housekeep_metrics(now)
        self.burst_id += 1
        self._bursts[self.burst_id] = [now, []]
        if self.mode == "sync":
            return self._tick_sync(now)
        return self._tick_priority(now)

    def _tick_sync(self, now):
        """Output frames: every changed bulb in the same burst, same transition."""
        for rt in self.bulbs.values():
            self._expire(rt, now)
        if now < self.frame_next:
            return 0
        active = [rt for rt in self.bulbs.values() if rt.ip and rt.target is not None]
        period = max(self.min_interval, len(active) / self.budget if active else self.min_interval)
        self.frame_period = period
        # Keep a steady cadence; if we fell behind (e.g. a stall), restart from now.
        self.frame_next = self.frame_next + period if now - self.frame_next < period else now + period
        trans = int(period * 1000) if self.adaptive else self.fixed_ms
        sent = 0
        for rt in active:
            # The frame period already respects min_interval. Only a backed-off
            # bulb sits out frames until its own (longer) interval has passed;
            # checking everyone's interval would make tick jitter skip frames.
            ready = not rt.backed_off(self.min_interval) or now - rt.last_send >= rt.interval
            if rt.dirty and ready:
                if self._send(rt, now, transition_ms=trans):
                    sent += 1
        room = int(self.budget * period) - sent
        if room > 0:
            stale = [rt for rt in active if not rt.dirty and not rt.confirmed
                     and now - rt.last_send >= max(self.refresh_s, rt.interval)]
            stale.sort(key=lambda rt: rt.last_send)
            for rt in stale[:room]:
                if self._send(rt, now, refresh=True):
                    sent += 1
        return sent

    def _tick_priority(self, now):
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
                     if rt.ip and rt.target is not None and not rt.dirty and not rt.confirmed
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
                "source": rt.source, "state": rt.target, "online": rt.online(now, self.offline_after),
                "rtt_ms": None if rt.rtt is None else round(rt.rtt * 1000, 1),
                "reply_rate": round(rt.replies / rt.sends, 3) if rt.sends else None,
                "interval_ms": round(rt.interval * 1000), "backoff": rt.backed_off(self.min_interval),
                "sends": rt.sends, "replies": rt.replies, "misses": rt.misses,
            }
        return out


HIST_EDGES_MS = (10, 20, 35, 50, 75, 100, 150, 250, 500, 1000)


def histogram(values_s, edges_ms=HIST_EDGES_MS):
    """Counts of values (seconds) in the buckets <edges[0], <edges[1], ... and >= the last edge."""
    counts = [0] * (len(edges_ms) + 1)
    for v in values_s:
        ms = v * 1000
        i = 0
        while i < len(edges_ms) and ms >= edges_ms[i]:
            i += 1
        counts[i] += 1
    return counts


def percentile(values, p):
    if not values:
        return None
    v = sorted(values)
    return v[min(len(v) - 1, int(len(v) * p))]
