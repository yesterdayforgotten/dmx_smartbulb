"""The engine's configuration: one JSON document, validated, saved power-safely.

Layout (see DEFAULTS for every setting):

    bulbs   {MAC: {name, ip, channel | follow, dmx, groups, pos}}
            channel: the bulb's own first DMX channel (1-510; uses N..N+2)
            follow:  a group name, to use that group's shared channel instead
    groups  {name: {channel}}   channel may be null (a group used only for selection)
    looks   {name: {MAC: {"h", "s", "v"} or {"k", "v"}}}   Kasa units
    input, sender, dmx_loss, network, auth, kasa_port, web_port

Saving keeps two copies (config.json and config.json.bak), each wrapped with a
sequence number and a SHA-256 of its contents, written to a temp file, fsynced
and renamed over the older copy. Loading takes the newest copy whose checksum
matches, so a power cut in the middle of a save loses at most that one change.
"""

import copy
import hashlib
import ipaddress
import json
import os
import re
import time
from pathlib import Path

DEFAULT_PATH = Path("/boot/firmware/dmx_smartbulb/config.json")
MAX_BULBS = 170          # 512 channels / 3
MAX_CHANNEL = 510        # a bulb uses N, N+1, N+2
CURVES = ("linear", "square", "scurve")
LOSS_MODES = ("hold", "look", "blackout")

DEFAULTS = {
    "bulbs": {},
    "groups": {},
    "looks": {},
    "input": {"backend": "uart", "port": "/dev/ttyAMA3"},
    "sender": {
        "budget_pps": 500,           # packets/s for the whole rig (clean for 50 bulbs at 10 Hz in tests)
        "min_interval_ms": 100,      # per bulb: at most 10 commands/s (firmware 1.0.15 handles 30)
        "max_backoff_ms": 1000,      # ceiling when a bulb stops replying
        "refresh_s": 2.0,            # re-send unconfirmed commands after this long
        "backoff_after": 3,          # consecutive lost replies before a bulb is slowed down
        "idle_check_s": 3.0,         # status-check bulbs not heard from for this long
        "wifi_priority": "off",      # off | video | voice: WiFi (WMM) priority of our packets
        "adaptive_transition": False,  # experimental: fade over the send interval
        "fixed_transition_ms": 30,   # fade time per update (the old app's 30 ms)
        "snap_threshold": 0.15,      # changes larger than this fraction snap (0 ms)
        "curve": "linear",
        "mode": "sync",              # sync: every changed bulb per output frame; priority: biggest change first
    },
    "dmx_loss": {"mode": "hold", "after_s": 5.0, "look": None},
    "identify": {"blink_hz": 2.0, "duration_s": 4.0},
    "network": {"ssid": "", "password": ""},
    "auth": {"password_hash": None, "session_secret": None},
    "kasa_port": 9999,
    "web_port": 80,
}

BULB_DEFAULTS = {"name": "", "ip": None, "channel": None, "follow": None,
                 "dmx": True, "groups": [], "pos": None}

_MAC = re.compile(r"^[0-9A-F]{12}$")


class ConfigError(ValueError):
    def __init__(self, problems):
        self.problems = list(problems)
        super().__init__("; ".join(self.problems))


def _merge(defaults, value):
    """Defaults filled in under value, recursively for dicts of settings."""
    out = copy.deepcopy(defaults)
    for k, v in (value or {}).items():
        if isinstance(out.get(k), dict) and isinstance(v, dict) and k not in ("bulbs", "groups", "looks"):
            out[k] = _merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _num(problems, where, value, lo, hi, integer=False):
    ok = isinstance(value, (int, float)) and not isinstance(value, bool)
    if ok and integer:
        ok = float(value).is_integer()
    if not ok or not lo <= value <= hi:
        problems.append(f"{where} must be {'an integer' if integer else 'a number'} from {lo} to {hi}")


def validate(raw):
    """Return a complete, checked config (defaults filled in) or raise ConfigError."""
    cfg = _merge(DEFAULTS, raw)
    problems = []

    groups = cfg["groups"]
    if not isinstance(groups, dict):
        problems.append("groups must be an object")
        groups = cfg["groups"] = {}
    for name, g in list(groups.items()):
        g = groups[name] = _merge({"channel": None}, g)
        if not name.strip():
            problems.append("group names can't be empty")
        if g["channel"] is not None:
            _num(problems, f"group {name!r} channel", g["channel"], 1, MAX_CHANNEL, integer=True)

    bulbs = cfg["bulbs"]
    if not isinstance(bulbs, dict):
        problems.append("bulbs must be an object")
        bulbs = cfg["bulbs"] = {}
    if len(bulbs) > MAX_BULBS:
        problems.append(f"at most {MAX_BULBS} bulbs (512 channels / 3); this config has {len(bulbs)}")
    seen_ips = {}
    for mac, b in list(bulbs.items()):
        b = bulbs[mac] = _merge(BULB_DEFAULTS, b)
        label = f"bulb {b['name'] or mac}"
        if not _MAC.match(mac):
            problems.append(f"{label}: key {mac!r} isn't a MAC (12 upper-case hex digits)")
        if b["ip"] is not None:
            try:
                ipaddress.IPv4Address(b["ip"])
            except ValueError:
                problems.append(f"{label}: {b['ip']!r} isn't an IPv4 address")
            if b["ip"] in seen_ips:
                problems.append(f"{label} and bulb {seen_ips[b['ip']]} have the same IP {b['ip']}")
            seen_ips[b["ip"]] = b["name"] or mac
        if b["channel"] is not None and b["follow"] is not None:
            problems.append(f"{label}: set either its own channel or a group to follow, not both")
        if b["channel"] is not None:
            _num(problems, f"{label} channel", b["channel"], 1, MAX_CHANNEL, integer=True)
        if b["follow"] is not None and b["follow"] not in groups:
            problems.append(f"{label} follows group {b['follow']!r}, which doesn't exist")
        if not isinstance(b["dmx"], bool):
            problems.append(f"{label}: dmx must be true or false")
        if not isinstance(b["groups"], list) or any(g not in groups for g in b["groups"]):
            problems.append(f"{label}: groups must be a list of existing group names")
        if b["pos"] is not None:
            p = b["pos"]
            if not (isinstance(p, list) and len(p) == 2 and all(isinstance(v, (int, float)) and 0 <= v <= 1 for v in p)):
                problems.append(f"{label}: pos must be [x, y] with each from 0 to 1, or null")

    for name, look in cfg["looks"].items():
        if not isinstance(look, dict):
            problems.append(f"look {name!r} must be an object")
            continue
        for mac, st in look.items():
            if mac not in bulbs:
                problems.append(f"look {name!r} refers to unknown bulb {mac}")
                continue
            if not isinstance(st, dict) or "v" not in st or not (("h" in st and "s" in st) or "k" in st):
                problems.append(f"look {name!r}, bulb {mac}: needs h, s, v or k, v")
                continue
            _num(problems, f"look {name!r} brightness", st["v"], 0, 100, integer=True)
            if "k" in st:
                _num(problems, f"look {name!r} color temperature", st["k"], 2500, 6500)
            else:
                _num(problems, f"look {name!r} hue", st["h"], 0, 360, integer=True)
                _num(problems, f"look {name!r} saturation", st["s"], 0, 100, integer=True)

    inp = cfg["input"]
    if inp["backend"] not in ("uart", "esp32"):
        problems.append("input backend must be 'uart' or 'esp32'")

    s = cfg["sender"]
    _num(problems, "send budget", s["budget_pps"], 1, 5000)
    _num(problems, "per-bulb minimum interval", s["min_interval_ms"], 10, 2000)
    _num(problems, "maximum backoff", s["max_backoff_ms"], s["min_interval_ms"] if isinstance(s["min_interval_ms"], (int, float)) else 10, 60000)
    _num(problems, "refresh interval", s["refresh_s"], 0.2, 60)
    _num(problems, "lost replies before backoff", s["backoff_after"], 1, 50, integer=True)
    _num(problems, "idle check period", s["idle_check_s"], 0.5, 300)
    _num(problems, "fixed transition", s["fixed_transition_ms"], 0, 10000, integer=True)
    _num(problems, "snap threshold", s["snap_threshold"], 0, 1)
    if not isinstance(s["adaptive_transition"], bool):
        problems.append("adaptive transition must be true or false")
    if s["wifi_priority"] not in ("off", "video", "voice"):
        problems.append("WiFi priority must be off, video or voice")
    if s["mode"] not in ("priority", "sync"):
        problems.append("send mode must be 'priority' or 'sync'")
    if s["curve"] not in CURVES:
        problems.append(f"brightness curve must be one of {', '.join(CURVES)}")

    loss = cfg["dmx_loss"]
    if loss["mode"] not in LOSS_MODES:
        problems.append(f"DMX-loss mode must be one of {', '.join(LOSS_MODES)}")
    _num(problems, "DMX-loss delay", loss["after_s"], 0, 3600)
    if loss["mode"] == "look" and loss["look"] not in cfg["looks"]:
        problems.append(f"DMX-loss look {loss['look']!r} doesn't exist")

    ident = cfg["identify"]
    _num(problems, "identify blink rate", ident["blink_hz"], 0.2, 10)
    _num(problems, "identify duration", ident["duration_s"], 0.5, 60)

    _num(problems, "Kasa port", cfg["kasa_port"], 1, 65535, integer=True)
    _num(problems, "web port", cfg["web_port"], 1, 65535, integer=True)

    if problems:
        raise ConfigError(problems)
    return cfg


def bulb_channel(cfg, mac):
    """The first DMX channel a bulb takes its color from, or None if unpatched."""
    b = cfg["bulbs"][mac]
    if b["follow"] is not None:
        return cfg["groups"][b["follow"]]["channel"]
    return b["channel"]


def patch_conflicts(cfg):
    """Warnings (not errors) for bulbs whose 3-channel ranges overlap. Bulbs
    following the same group share a range on purpose and aren't reported."""
    users = {}  # channel -> set of owners ("group X" or a bulb name)
    for mac, b in cfg["bulbs"].items():
        ch = bulb_channel(cfg, mac)
        if ch is None:
            continue
        owner = f"group {b['follow']}" if b["follow"] else (b["name"] or mac)
        for c in range(ch, ch + 3):
            users.setdefault(c, set()).add(owner)
    warnings, seen = [], set()
    for c in sorted(users):
        owners = tuple(sorted(users[c]))
        if len(owners) > 1 and owners not in seen:
            seen.add(owners)
            warnings.append(f"Overlap: channel {c} is used by {', '.join(owners)}")
    for mac, b in cfg["bulbs"].items():
        if b["follow"] and cfg["groups"].get(b["follow"], {}).get("channel") is None:
            warnings.append(f"{b['name'] or mac} follows group {b['follow']}, which has no channel, so it gets no DMX")
    return warnings


def next_free_channel(cfg, start=1):
    """Lowest channel >= start where three channels are unused, or None."""
    used = set()
    for mac in cfg["bulbs"]:
        ch = bulb_channel(cfg, mac)
        if ch is not None:
            used.update(range(ch, ch + 3))
    for g in cfg["groups"].values():
        if g["channel"] is not None:
            used.update(range(g["channel"], g["channel"] + 3))
    for ch in range(max(1, start), MAX_CHANNEL + 1):
        if not used & {ch, ch + 1, ch + 2}:
            return ch
    return None


def _digest(cfg):
    return hashlib.sha256(json.dumps(cfg, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class ConfigStore:
    def __init__(self, path=DEFAULT_PATH):
        self.path = Path(path)
        self.bak = self.path.with_name(self.path.name + ".bak")
        self.seq = 0

    def _read(self, path):
        try:
            env = json.loads(path.read_text())
            if env.get("sha256") == _digest(env["config"]):
                return env
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            pass
        return None

    def load(self):
        """Newest valid copy, validated. No files at all means a fresh install:
        defaults. Files that exist but are all damaged raise, so a corrupt card
        is noticed instead of silently replaced with an empty config."""
        copies = [e for e in (self._read(self.path), self._read(self.bak)) if e]
        if not copies:
            if self.path.exists() or self.bak.exists():
                raise ConfigError([f"{self.path} and {self.bak.name} are both unreadable or damaged"])
            self.seq = 0
            return validate({})
        newest = max(copies, key=lambda e: e["seq"])
        self.seq = newest["seq"]
        return validate(newest["config"])

    def save(self, cfg):
        """Validate and write over the older copy. Returns the saved config."""
        cfg = validate(cfg)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        current = self._read(self.path)
        # Overwrite whichever copy is not the newest valid one.
        target = self.bak if current and current["seq"] == self.seq else self.path
        env = {"seq": self.seq + 1, "saved": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
               "sha256": _digest(cfg), "config": cfg}
        tmp = target.with_name(target.name + ".tmp")
        with open(tmp, "w") as f:
            json.dump(env, f, indent=1, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, target)
        dirfd = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(dirfd)
        finally:
            os.close(dirfd)
        self.seq += 1
        return cfg
