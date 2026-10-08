"""Control Board: sends DMX out through an Enttec Open DMX USB, like a console.

When an Enttec (FTDI FT232R) is plugged in, the web UI's Control Board tab can
set channels, set a color for chosen fixtures, or run test shows. The 512
channel values live in `values`; a background thread sends them as DMX frames
(BREAK, MAB, start code 0 and 512 slots at 250 kbaud) back to back, about 25
times a second. That is the ceiling for full 512-slot frames here: the frame takes
23 ms on the wire, and the FTDI driver takes 15-28 ms just to start the BREAK.
The DMX goes out of the Enttec into the show's DMX line, back in through the
receiver, so everything downstream behaves exactly as it would with a desk.
"""

import asyncio
import colorsys
import glob
import logging
import math
import random
import threading
import time

from engine.sender import WHITE_K

log = logging.getLogger("board")

ENTTEC_GLOB = "/dev/serial/by-id/usb-FTDI_FT232R*"
SLOT_S = 11 / 250000          # one slot (start + 8 data + 2 stop bits)
SHOWS = {
    "rainbow": "Rainbow, all together",
    "chase_rainbow": "Rainbow chase (each fixture offset)",
    "breathe": "Breathe (brightness swell)",
    "snaps": "Color snaps (R, G, B, white)",
    "random": "Random color snaps",
    "chase": "Chase (one at a time, channel order)",
    "flash": "Flash (all on/off)",
}


def find_enttec():
    ports = sorted(glob.glob(ENTTEC_GLOB))
    return ports[0] if ports else None


def hsv_to_dmx(h, s, v):
    """Kasa-style hue (0-360), saturation and brightness (0-100) to the three
    DMX bytes the engine reads (hue 0-255, saturation 0-255, brightness 0-255)."""
    return (round(h / 360 * 255) % 256, round(s / 100 * 255), round(v / 100 * 255))


class Board:
    def __init__(self, fixtures):
        """fixtures: callable returning [(label, start channel, channels)] for
        patched addresses (solo bulbs and group channels), in channel order.
        channels is 3 (HSI) or 4 (HSIC) and may be left out (3)."""
        self.fixtures = fixtures
        self.values = bytearray(512)
        self.port = None
        self.active = False           # sending frames
        self.runs = {}                # running shows by id (see start_show)
        self._run_seq = 0
        self.frames = 0
        self.error = None
        self._thread = None
        self._stop = threading.Event()
        self._lock = threading.Lock()

    # ---- output ---------------------------------------------------------

    def available(self):
        return find_enttec() is not None

    def start(self):
        """Begin sending (idempotent). Raises RuntimeError if no Enttec."""
        if self.active:
            return
        port = find_enttec()
        if port is None:
            raise RuntimeError("no Enttec Open DMX USB found")
        self.port = port
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="board-out", daemon=True)
        self._thread.start()
        self.active = True
        log.info("Control Board sending DMX on %s", port)

    def release(self):
        """Stop the show and stop sending, so a real desk can take over."""
        self.stop_show()
        self._stop.set()
        if self._thread:
            self._thread.join(2)
        self.active = False

    def _run(self):
        import serial
        try:
            ser = serial.Serial(self.port, baudrate=250000, bytesize=8, parity="N", stopbits=2, write_timeout=1)
            ser.rts = False           # some Open DMX units gate the line driver on RTS
        except (serial.SerialException, OSError) as e:
            self.error = str(e)
            self.active = False
            log.error("can't open %s: %s", self.port, e)
            return
        self.error = None
        try:
            while not self._stop.is_set():
                t0 = time.monotonic()
                with self._lock:
                    data = b"\x00" + bytes(self.values)
                try:
                    ser.break_condition = True
                    time.sleep(0.00012)
                    ser.break_condition = False
                    time.sleep(0.00002)
                    t_write = time.monotonic()
                    ser.write(data)
                    ser.flush()
                    # flush() only empties the kernel buffer; let the FTDI chip finish.
                    remaining = len(data) * SLOT_S + 0.003 - (time.monotonic() - t_write)
                    if remaining > 0:
                        time.sleep(remaining)
                    self.frames += 1
                except serial.SerialTimeoutException:
                    ser.reset_output_buffer()
                    ser.break_condition = False
                # Back to back (~25 frames/s; see the module docstring). The short
                # pause keeps a stalled USB write from spinning.
                if time.monotonic() - t0 < 0.015:
                    time.sleep(0.005)
        except (serial.SerialException, OSError) as e:
            self.error = str(e)
            log.error("Control Board output stopped: %s", e)
        finally:
            self.active = False
            try:
                ser.close()
            except Exception:  # noqa: BLE001 - closing a vanished port
                pass

    # ---- values ---------------------------------------------------------------

    def set_channels(self, changes):
        """changes: {channel (1-512): value (0-255)}. Shows let go of the fixtures touched."""
        changes = {int(ch): int(v) for ch, v in changes.items()}
        self._take_over({ch - off for ch in changes for off in range(4)})
        with self._lock:
            for ch, v in changes.items():
                if 1 <= ch <= 512:
                    self.values[ch - 1] = max(0, min(255, v))

    def _fixtures(self):
        return [(f[0], f[1], f[2] if len(f) > 2 else 3) for f in self.fixtures()]

    def set_fixture_white(self, channels, k, v):
        """White at a color temperature. HSIC fixtures get real white (saturation 0
        and the temperature channel); HSI ones only have color, so they get a pale
        tint toward orange (warm) or blue (cool)."""
        self._take_over(set(channels))
        sizes = {ch: size for _, ch, size in self._fixtures()}
        lo, hi = WHITE_K
        cct = round(max(0, min(1, (k - lo) / (hi - lo))) * 255)
        warm = max(0.0, min(1.0, (6500 - k) / 4000))
        h, sat = (30, round(35 * warm)) if warm > 0.15 else (220, round(15 * (1 - warm)))
        with self._lock:
            for ch in channels:
                if sizes.get(ch) == 4 and 1 <= ch <= 509:
                    self.values[ch - 1:ch + 3] = bytes((0, 0, round(v / 100 * 255), cct))
                elif 1 <= ch <= 510:
                    self.values[ch - 1:ch + 2] = bytes(hsv_to_dmx(h, sat, v))

    def set_fixture_cct(self, channels, k):
        """Set only the color-temperature channel of the HSIC fixtures among
        `channels`; HSI fixtures have none and are left alone. Shows never write
        this channel, so they keep running."""
        lo, hi = WHITE_K
        cct = round(max(0, min(1, (k - lo) / (hi - lo))) * 255)
        hsic = [ch for _, ch, size in self._fixtures() if size == 4 and ch in set(channels) and ch <= 509]
        with self._lock:
            for ch in hsic:
                self.values[ch + 2] = cct
        return len(hsic)

    def set_fixture_color(self, channels, h, s, v):
        """Write one color to each fixture's three channels."""
        self._take_over(set(channels))
        trio = hsv_to_dmx(h, s, v)
        with self._lock:
            for ch in channels:
                if 1 <= ch <= 510:
                    self.values[ch - 1:ch + 2] = bytes(trio)

    def blackout(self):
        self.stop_show()
        with self._lock:
            for i in range(len(self.values)):
                self.values[i] = 0

    def patched_values(self):
        out = []
        for label, ch, size in self._fixtures():
            f = {"label": label, "channel": ch, "size": size, "h": self.values[ch - 1], "s": self.values[ch],
                 "v": self.values[ch + 1]}
            if size == 4 and ch <= 509:
                f["c"] = self.values[ch + 2]
            out.append(f)
        return out

    # ---- shows ---------------------------------------------------------------

    # Several shows can run at once, each on its own fixtures (all of them, or a
    # group's). A run is {"id", "name", "speed", "target", "channels", "task"};
    # channels None means every patched fixture.

    @property
    def show(self):
        """The first running show's name (or None)."""
        return next(iter(self.runs.values()))["name"] if self.runs else None

    def _all_channels(self):
        return {ch for _, ch, _ in self._fixtures()}

    def _take_over(self, channels):
        """Remove fixtures from the shows running on them; stop shows left with none."""
        if not channels:
            return
        for rid, run in list(self.runs.items()):
            have = self._all_channels() if run["channels"] is None else run["channels"]
            left = have - set(channels)
            if left == have:
                continue
            if left:
                run["channels"] = left
            else:
                self.stop_show(rid)

    def start_show(self, name, speed=1.0, target="all", channels=None):
        """Run a show on `channels` (None: all fixtures), labeled `target`. Starting
        the same show on the same target only changes its speed."""
        if name not in SHOWS:
            raise ValueError(f"unknown show {name!r}")
        speed = max(0.1, min(10.0, float(speed)))
        for run in self.runs.values():
            if run["target"] == target and run["name"] == name:
                run["speed"] = speed
                return run["id"]
        if channels is None:
            self.stop_show()
        else:
            channels = set(channels)
            self._take_over(channels)
        for rid, run in list(self.runs.items()):
            if run["target"] == target:
                self.stop_show(rid)
        self._run_seq += 1
        rid = self._run_seq
        run = {"id": rid, "name": name, "speed": speed, "target": target, "channels": channels}
        run["task"] = asyncio.get_running_loop().create_task(self._run_show(run))
        self.runs[rid] = run
        return rid

    def stop_show(self, run_id=None):
        """Stop one show, or all of them."""
        for rid in ([run_id] if run_id is not None else list(self.runs)):
            run = self.runs.pop(rid, None)
            if run and not run["task"].done():
                run["task"].cancel()

    async def _run_show(self, run):
        name = run["name"]
        rng = random.Random()
        t, last = 0.0, time.monotonic()
        last_step = -1
        randoms = {}
        try:
            while True:
                now = time.monotonic()
                t += (now - last) * run["speed"]           # speed can change while running
                last = now
                mine = run["channels"]
                fixtures = [ch for _, ch, _ in self._fixtures() if mine is None or ch in mine]
                n = max(1, len(fixtures))
                frame = {}
                if name == "rainbow":
                    for ch in fixtures:
                        frame[ch] = ((t * 36) % 360, 100, 100)          # one turn every 10 s at speed 1
                elif name == "chase_rainbow":
                    for i, ch in enumerate(fixtures):
                        frame[ch] = ((t * 36 + i * 360 / n) % 360, 100, 100)
                elif name == "breathe":
                    v = 50 - 50 * math.cos(t * 2 * math.pi / 4)          # 4 s per swell
                    for ch in fixtures:
                        frame[ch] = (0, 0, v)
                elif name == "snaps":
                    step = int(t) % 4
                    color = [(0, 100, 100), (120, 100, 100), (240, 100, 100), (0, 0, 100)][step]
                    for ch in fixtures:
                        frame[ch] = color
                elif name == "random":
                    step = int(t / 2)                                     # every 2 s at speed 1
                    if step != last_step:
                        last_step = step
                        randoms = {ch: (rng.randrange(360), 100, 100) for ch in fixtures}
                    frame = {ch: randoms.get(ch, (0, 0, 0)) for ch in fixtures}
                elif name == "chase":
                    lit = int(t * 2) % n                                  # 2 fixtures per second
                    for i, ch in enumerate(fixtures):
                        frame[ch] = (0, 0, 100 if i == lit else 3)
                elif name == "flash":
                    on = int(t * 2) % 2 == 0                              # 1 flash per second
                    for ch in fixtures:
                        frame[ch] = (0, 0, 100 if on else 0)
                with self._lock:
                    for ch, (h, s, v) in frame.items():
                        if 1 <= ch <= 510:
                            self.values[ch - 1:ch + 2] = bytes(hsv_to_dmx(h, s, v))
                await asyncio.sleep(1 / 30)
        except asyncio.CancelledError:
            pass

    def status(self):
        runs = [{"id": r["id"], "name": r["name"], "label": SHOWS[r["name"]], "speed": r["speed"],
                 "target": r["target"],
                 "fixtures": len(self._all_channels() if r["channels"] is None else r["channels"])}
                for r in self.runs.values()]
        return {"available": self.available(), "active": self.active, "port": self.port, "show": self.show,
                "runs": runs, "frames": self.frames, "error": self.error,
                "shows": [{"id": k, "label": v} for k, v in SHOWS.items()]}
