"""Control Board: sends DMX out through an Enttec Open DMX USB, like a console.

When an Enttec (FTDI FT232R) is plugged in, the web UI's Control Board tab can
set channels, set a colour for chosen fixtures, or run test shows. The 512
channel values live in `values`; a background thread sends them as DMX frames
(BREAK, MAB, start code 0 and 512 slots at 250 kbaud) about 30 times a second.
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

log = logging.getLogger("board")

ENTTEC_GLOB = "/dev/serial/by-id/usb-FTDI_FT232R*"
SLOT_S = 11 / 250000          # one slot (start + 8 data + 2 stop bits)
SHOWS = {
    "rainbow": "Rainbow, all together",
    "chase_rainbow": "Rainbow chase (each fixture offset)",
    "breathe": "Breathe (brightness swell)",
    "snaps": "Colour snaps (R, G, B, white)",
    "random": "Random colour snaps",
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
        """fixtures: callable returning [(label, start channel)] for patched
        addresses (solo bulbs and group channels), in channel order."""
        self.fixtures = fixtures
        self.values = bytearray(512)
        self.port = None
        self.active = False           # sending frames
        self.show = None              # name of the running show
        self.speed = 1.0
        self.frames = 0
        self.error = None
        self._thread = None
        self._stop = threading.Event()
        self._show_task = None
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
                # About 30 frames/s leaves the CPU and USB some room.
                wait = 1 / 30 - (time.monotonic() - t0)
                if wait > 0:
                    time.sleep(wait)
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
        """changes: {channel (1-512): value (0-255)}."""
        self.stop_show()
        with self._lock:
            for ch, v in changes.items():
                ch, v = int(ch), int(v)
                if 1 <= ch <= 512:
                    self.values[ch - 1] = max(0, min(255, v))

    def set_fixture_colour(self, channels, h, s, v):
        """Write one colour to each fixture's three channels."""
        self.stop_show()
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
        for label, ch in self.fixtures():
            out.append({"label": label, "channel": ch, "h": self.values[ch - 1], "s": self.values[ch],
                        "v": self.values[ch + 1]})
        return out

    # ---- shows ---------------------------------------------------------------

    def start_show(self, name, speed=1.0):
        if name not in SHOWS:
            raise ValueError(f"unknown show {name!r}")
        self.stop_show()
        self.show, self.speed = name, max(0.1, min(10.0, float(speed)))
        self._show_task = asyncio.get_running_loop().create_task(self._run_show(name))

    def stop_show(self):
        if self._show_task and not self._show_task.done():
            self._show_task.cancel()
        self._show_task = None
        self.show = None

    async def _run_show(self, name):
        rng = random.Random()
        t0 = time.monotonic()
        last_step = -1
        randoms = {}
        try:
            while True:
                t = (time.monotonic() - t0) * self.speed
                fixtures = [ch for _, ch in self.fixtures()]
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
                    colour = [(0, 100, 100), (120, 100, 100), (240, 100, 100), (0, 0, 100)][step]
                    for ch in fixtures:
                        frame[ch] = colour
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
        return {"available": self.available(), "active": self.active, "port": self.port, "show": self.show,
                "speed": self.speed, "frames": self.frames, "error": self.error,
                "shows": [{"id": k, "label": v} for k, v in SHOWS.items()]}
