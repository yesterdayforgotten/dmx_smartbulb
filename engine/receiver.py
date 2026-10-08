"""DMX receiver: a child process that reads the input and publishes the latest
512 channels to shared memory, so the main process's work (bulb sends, web)
can never starve it.

Inputs:
    uart    direct DMX on a Pi UART (engine/dmx_uart.py)
    esp32   the ESP32's "DMX#" frames at 921600 baud (engine/dmx_esp32.py)
    replay  a recording made with `dmx_rx_probe.py --record`, played back in
            real time and looped (for development without hardware)

The main process owns a Receiver: it starts the child, restarts it if it dies
(counting restarts), and reads frames with snapshot(). The child writes a byte
to a pipe for every frame, so the main loop can wake on new data instead of
polling.
"""

import fcntl
import logging
import multiprocessing as mp
import os
import select
import signal
import struct
import time

from engine.dmx_uart import DmxParser, open_dmx_port

log = logging.getLogger("receiver")

TIOCGICOUNT = 0x545D
# struct serial_icounter_struct: cts dsr rng dcd rx tx frame overrun parity brk
# buf_overrun, then 9 reserved ints.
_ICOUNT = struct.Struct("20i")

STATS = ("frames", "held", "malformed", "error_bytes", "other_sc", "slots",
         "oe", "fe", "brk", "resyncs")
_IDX = {name: i for i, name in enumerate(STATS)}


def read_icount(fd):
    """Kernel line counters for this port (no root needed), or None."""
    buf = bytearray(_ICOUNT.size)
    try:
        fcntl.ioctl(fd, TIOCGICOUNT, buf)
    except OSError:
        return None
    v = _ICOUNT.unpack(buf)
    return {"rx": v[4], "fe": v[6], "oe": v[7], "brk": v[9], "buf_oe": v[10]}


class GlitchGuard:
    """Hold back a packet whose length differs from the last accepted one until
    the new length shows up twice in a row. A flaky cable can produce a junk
    packet that happens to start with 0x00 and has no framing errors; it would
    almost never also repeat the junk length. A real change of slot count costs
    one packet of delay."""

    def __init__(self):
        self.length = None
        self.pending = None
        self.held = 0

    def accept(self, slots):
        n = len(slots)
        if self.length is None or n == self.length or (self.pending is not None and len(self.pending) == n):
            self.length = n
            self.pending = None
            return slots
        self.pending = slots
        self.held += 1
        return None


class Shared:
    """Shared memory between the receiver child and the main process."""

    def __init__(self, ctx):
        self.lock = ctx.Lock()
        self.data = ctx.RawArray("B", 512)
        self.seq = ctx.RawValue("Q", 0)
        self.t = ctx.RawValue("d", 0.0)      # time.monotonic() of the last frame
        self.stats = ctx.RawArray("q", len(STATS))
        self.wake_r, self.wake_w = os.pipe()
        for fd in (self.wake_r, self.wake_w):
            os.set_blocking(fd, False)

    def publish(self, slots):
        n = len(slots)
        with self.lock:
            self.data[:n] = slots       # a short packet leaves the channels above it as they were
            self.seq.value += 1
            self.t.value = time.monotonic()
        self.stats[_IDX["slots"]] = n
        try:
            os.write(self.wake_w, b".")
        except BlockingIOError:
            pass                        # the main process is behind; one wake-up is enough

    def set(self, name, value):
        self.stats[_IDX[name]] = value


def _child(backend, port, shared, rt_priority):
    signal.signal(signal.SIGINT, signal.SIG_IGN)   # the parent handles Ctrl-C
    os.close(shared.wake_r)
    if rt_priority:
        try:
            os.sched_setscheduler(0, os.SCHED_FIFO, os.sched_param(rt_priority))
        except PermissionError:
            log.warning("no permission for real-time priority; running at normal priority")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s receiver %(levelname)s %(message)s")
    try:
        {"uart": _run_uart, "esp32": _run_esp32, "replay": _run_replay}[backend](port, shared)
    except Exception:
        log.exception("receiver stopped")
        raise


def _report_icount(fd, shared, last):
    """Copy kernel counters into the stats and log any new overruns/framing errors."""
    ic = read_icount(fd)
    if ic is None:
        return last
    if last is not None:
        if ic["oe"] > last["oe"] or ic["buf_oe"] > last["buf_oe"]:
            log.warning("UART overrun: %d new (hardware FIFO), %d new (tty buffer)",
                        ic["oe"] - last["oe"], ic["buf_oe"] - last["buf_oe"])
        if ic["fe"] > last["fe"]:
            log.info("%d framing errors in the last second", ic["fe"] - last["fe"])
    for k in ("oe", "fe", "brk"):
        shared.set(k, ic[k])
    return ic


def _run_uart(port, shared):
    fd = open_dmx_port(port)
    parser, guard = DmxParser(), GlitchGuard()
    log.info("reading direct DMX on %s", port)
    last_ic, next_report = None, 0.0
    while True:
        if select.select([fd], [], [], 0.5)[0]:
            for f in parser.feed(os.read(fd, 4096)):
                if f.start_code == 0:
                    slots = guard.accept(f.slots)
                    if slots is not None:
                        shared.publish(slots)
        now = time.monotonic()
        if now >= next_report:
            next_report = now + 1.0
            s = parser.stats
            shared.set("frames", s.frames)
            shared.set("held", guard.held)
            shared.set("malformed", s.malformed)
            shared.set("error_bytes", s.error_bytes)
            shared.set("other_sc", sum(s.other_start_codes.values()))
            last_ic = _report_icount(fd, shared, last_ic)


def _run_esp32(port, shared):
    import serial
    from engine.dmx_esp32 import ESP32_BAUD, Esp32Framer
    ser = serial.Serial(port, ESP32_BAUD, timeout=0.5)
    framer = Esp32Framer()
    log.info("reading ESP32 frames on %s", port)
    last_ic, next_report = None, 0.0
    while True:
        data = ser.read(max(1, ser.in_waiting))
        for frame in framer.feed(data):
            shared.publish(frame)
        now = time.monotonic()
        if now >= next_report:
            next_report = now + 1.0
            shared.set("frames", framer.frames)
            shared.set("resyncs", framer.resyncs)
            last_ic = _report_icount(ser.fileno(), shared, last_ic)


def _run_replay(path, shared, rate=22600):
    """Play a PARMRK recording back at about the real DMX byte rate, looping."""
    data = open(path, "rb").read()
    if not data:
        raise ValueError(f"{path} is empty")
    parser, guard = DmxParser(), GlitchGuard()
    log.info("replaying %s (%d bytes, looped)", path, len(data))
    chunk = 256
    start, sent = time.monotonic(), 0
    while True:
        for i in range(0, len(data), chunk):
            for f in parser.feed(data[i:i + chunk]):
                if f.start_code == 0:
                    slots = guard.accept(f.slots)
                    if slots is not None:
                        shared.publish(slots)
            sent += chunk
            shared.set("frames", parser.stats.frames)
            ahead = sent / rate - (time.monotonic() - start)
            if ahead > 0:
                time.sleep(ahead)


class Receiver:
    def __init__(self, backend, port, rt_priority=0):
        self.backend, self.port, self.rt_priority = backend, port, rt_priority
        self.ctx = mp.get_context("fork")
        self.shared = Shared(self.ctx)
        self.proc = None
        self.restarts = 0
        self._next_start = 0.0
        self._backoff = 1.0

    @property
    def wake_fd(self):
        return self.shared.wake_r

    def start(self):
        self.proc = self.ctx.Process(target=_child, name=f"dmx-receiver-{self.backend}", daemon=True,
                                     args=(self.backend, self.port, self.shared, self.rt_priority))
        self.proc.start()
        self._started = time.monotonic()

    def check(self):
        """Restart the child if it has died. Call about once a second."""
        if self.proc is not None and self.proc.is_alive():
            if time.monotonic() - self._started > 30:
                self._backoff = 1.0      # it has run for a while; reset the backoff
            return True
        now = time.monotonic()
        if now < self._next_start:
            return False
        if self.proc is not None:
            log.warning("receiver exited (code %s); restarting", self.proc.exitcode)
            self.restarts += 1
        self._next_start = now + self._backoff
        self._backoff = min(self._backoff * 2, 30.0)
        self.start()
        return False

    def drain(self):
        """Clear pending wake-up bytes."""
        try:
            while os.read(self.shared.wake_r, 4096):
                pass
        except BlockingIOError:
            pass

    def snapshot(self):
        """(sequence number, monotonic time of the frame, 512 channel bytes)."""
        s = self.shared
        with s.lock:
            return s.seq.value, s.t.value, bytes(s.data)

    def stats(self):
        out = {name: self.shared.stats[i] for i, name in enumerate(STATS)}
        out["restarts"] = self.restarts
        out["alive"] = bool(self.proc and self.proc.is_alive())
        return out

    def stop(self):
        if self.proc is not None and self.proc.is_alive():
            self.proc.terminate()
            self.proc.join(2)
            if self.proc.is_alive():
                self.proc.kill()
                self.proc.join(1)
