"""Test recorder: the Record button saves what the engine sees to the SD card, so a
test at the venue can be analyzed afterward. Logs are otherwise kept in RAM and lost
on a reboot.

A recording is one JSON object per line in /var/lib/dmx_smartbulb/recordings/:
  start    the patch and settings (no passwords)
  status   every second: engine status, as in the status file
  dmx      every change in the incoming DMX, timed to the frame: "values" (all 512, hex)
           at the start, every 10 s and when most channels change, otherwise "changes"
           [channel, value, ...]. "seq" is the receiver's frame number. dmx_frames()
           rebuilds the frames from these, so a console's cues can be reproduced.
  log      each log message
  stop     why it ended; missing if the power was cut

t is seconds since the start. The Pi has no battery clock, so without internet the
wall-clock times, and the times in file names, can be days out.

A thread does the writing, so a slow SD card can't hold up DMX. At most 5 s of data
is lost if the power is cut.
"""

import asyncio
import json
import logging
import os
import queue
import re
import threading
import time
from pathlib import Path

log = logging.getLogger("recorder")

NAME = re.compile(r"recording-[0-9-]+\.jsonl")
SAMPLE_S = 1.0
SYNC_S = 5.0                 # fsync this often
MAX_BYTES = 2_000_000_000    # one recording stops itself at this size
QUEUE_MAX = 5000
CONFIG_SECTIONS = ("bulbs", "groups", "sender", "input", "dmx_loss")
KEYFRAME_S = 10.0            # all 512 values this often, so a dropped row can't spoil the rest
FULL_ROW_CHANGES = 150       # past this many changed channels, a hex row is the smaller one


def dmx_frames(path):
    """Yield (t, seq, 512 channel bytes) for each DMX change in a recording. A last line
    cut off by a power failure is skipped."""
    values = None
    with open(path, encoding="utf-8") as f:
        for line in f:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if row.get("type") != "dmx":
                continue
            if "values" in row:
                values = bytearray.fromhex(row["values"])
            elif values is not None:
                changes = row["changes"]
                for i in range(0, len(changes), 2):
                    values[changes[i] - 1] = changes[i + 1]
            else:
                continue
            yield row["t"], row["seq"], bytes(values)


class _LogTap(logging.Handler):
    def __init__(self, recorder):
        super().__init__(logging.INFO)
        self.recorder = recorder
        self.pid = os.getpid()

    def emit(self, record):
        # The DMX receiver is a forked child and inherits this handler; it must never
        # touch the queue, whose lock a thread here may have held at the fork.
        if os.getpid() == self.pid:
            self.recorder._add({"type": "log", "level": record.levelname, "name": record.name,
                                "msg": record.getMessage()})


class Recorder:
    def __init__(self, engine, directory):
        self.engine = engine
        self.dir = Path(directory)
        self.name = None             # the file being written
        self.error = None            # why the last recording stopped by itself
        self._t0 = 0.0
        self._bytes = 0
        self._dropped = 0
        self._queue = None
        self._thread = None
        self._task = None
        self._tap = None
        self._last_dmx = None
        self._last_key = 0.0

    @property
    def active(self):
        return self.name is not None

    def start(self):
        if self.active:
            return self.name
        self.dir.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        path = self.dir / f"recording-{stamp}.jsonl"
        n = 1
        while path.exists():         # the clock can repeat itself after a reboot without internet
            n += 1
            path = self.dir / f"recording-{stamp}-{n}.jsonl"
        f = open(path, "w", encoding="utf-8")
        self.name, self.error = path.name, None
        self._t0, self._bytes, self._dropped = time.monotonic(), 0, 0
        self._last_dmx = None
        self._queue = queue.Queue(QUEUE_MAX)
        self._thread = threading.Thread(target=self._write, args=(f, self._queue), name="recorder", daemon=True)
        self._thread.start()
        self._add({"type": "start", "wall": time.time(),
                   "config": {k: self.engine.cfg[k] for k in CONFIG_SECTIONS}})
        self._tap = _LogTap(self)
        logging.getLogger().addHandler(self._tap)
        self._task = asyncio.get_running_loop().create_task(self._sample())
        log.info("recording to %s", path.name)
        return self.name

    def stop(self, reason="stopped"):
        if not self.active:
            return
        name = self.name
        log.info("recording stopped: %s (%s)", name, reason)
        if self._tap:
            logging.getLogger().removeHandler(self._tap)
            self._tap = None
        if self._task and self._task is not _current_task():
            self._task.cancel()
        self._add({"type": "stop", "reason": reason, "dropped": self._dropped})
        self._queue.put(None)
        self.name = None

    def join(self, timeout=5.0):
        """Wait for the writer to finish; call off the event loop if it's running."""
        if self._thread:
            self._thread.join(timeout)

    def status(self):
        return {"active": self.active, "name": self.name, "error": self.error, "bytes": self._bytes,
                "seconds": round(time.monotonic() - self._t0) if self.active else 0}

    def list(self):
        """Saved recordings, newest first (by modification order, not name)."""
        if not self.dir.is_dir():
            return []
        files = [p for p in self.dir.iterdir() if NAME.fullmatch(p.name)]
        return [{"name": p.name, "bytes": p.stat().st_size, "active": p.name == self.name}
                for p in sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)]

    def path_of(self, name):
        path = self.dir / name
        return path if NAME.fullmatch(name) and path.is_file() else None

    def on_frame(self, seq, t, data):
        """Called by the engine for each new DMX frame; t is the frame's monotonic time."""
        last = self._last_dmx
        if last is None or t - self._last_key >= KEYFRAME_S:
            self._last_key = t
            self._last_dmx = data
            self._add({"type": "dmx", "seq": seq, "values": data.hex()}, t)
        elif data != last:
            self._last_dmx = data
            changes = [i for i, (a, b) in enumerate(zip(last, data)) if a != b]
            if len(changes) > FULL_ROW_CHANGES:
                self._add({"type": "dmx", "seq": seq, "values": data.hex()}, t)
            else:
                flat = []
                for i in changes:
                    flat += (i + 1, data[i])
                self._add({"type": "dmx", "seq": seq, "changes": flat}, t)

    def _add(self, row, t=None):
        if self._queue is None:
            return
        row["t"] = round((time.monotonic() if t is None else t) - self._t0, 4)
        # Encoded here, on the caller's thread: the status dicts are live engine state.
        try:
            self._queue.put_nowait(json.dumps(row, separators=(",", ":")))
        except queue.Full:
            self._dropped += 1

    async def _sample(self):
        while True:
            self._add({"type": "status", "wall": time.time(), "status": self.engine.status()})
            if self._bytes >= MAX_BYTES:
                self.error = f"stopped by itself at {MAX_BYTES // 1_000_000} MB"
            if self.error:
                self.stop(self.error)
                return
            await asyncio.sleep(SAMPLE_S)

    def _write(self, f, q):
        last_sync = time.monotonic()
        with f:
            while (line := q.get()) is not None:
                try:
                    f.write(line + "\n")
                    self._bytes += len(line) + 1
                    if time.monotonic() - last_sync >= SYNC_S:
                        f.flush()
                        os.fsync(f.fileno())
                        last_sync = time.monotonic()
                except OSError as e:          # e.g. the card is full: _sample stops the recording
                    self.error = f"couldn't write to the SD card: {e}"
                    while q.get() is not None:
                        pass
                    return
            try:
                f.flush()
                os.fsync(f.fileno())
            except OSError as e:
                self.error = f"couldn't write to the SD card: {e}"


def _current_task():
    try:
        return asyncio.current_task()
    except RuntimeError:
        return None
