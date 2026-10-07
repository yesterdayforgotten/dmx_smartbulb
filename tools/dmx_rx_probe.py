#!/usr/bin/env python3
"""Measure direct DMX reception on a Pi UART.

Run with the dmx_smartbulb service stopped, under sudo (the kernel's oe/fe/brk
counters in /proc/tty/driver/ttyAMA are root-only):

    sudo python3 tools/dmx_rx_probe.py                     # live stats
    sudo python3 tools/dmx_rx_probe.py --record cap.bin    # also save the raw stream
    python3 tools/dmx_rx_probe.py --selftest [cap.bin ...] # replay captures + generated streams
    sudo python3 tools/dmx_rx_probe.py --crosscheck /dev/ttyUSB0 --pattern 0 --duration 600

With --crosscheck the source must be the dmx_esp32_txtest firmware: every packet
is checked byte for byte against tools/dmx_testpattern.py, gaps in its counter
are counted as lost, and the ESP32's own status lines over USB serial are
compared with what arrived.
"""

import argparse
import os
import random
import re
import select
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine.dmx_uart import DmxParser, ParserStats, open_dmx_port  # noqa: E402
from tools import dmx_testpattern as tp  # noqa: E402

PROC_TTY = "/proc/tty/driver/ttyAMA"


def kernel_counters(line_no):
    """Return {'rx':..,'oe':..,'fe':..,'brk':..} for one ttyAMA line, or None."""
    try:
        with open(PROC_TTY) as f:
            for row in f:
                if row.startswith(f"{line_no}:"):
                    # The kernel prints these as signed 32-bit ints, so rx goes
                    # negative after 2 GB; keep them unsigned so deltas work.
                    c = {k: int(v) & 0xFFFFFFFF for k, v in re.findall(r"(\w+):(-?\d+)", row)}
                    return {k: c.get(k, 0) for k in ("rx", "oe", "fe", "brk", "pe")}
    except PermissionError:
        return None
    return None


class Verifier:
    """Tracks test-pattern packets: corrupt, lost (counter gaps), restarts."""

    def __init__(self):
        self.ok = self.corrupt = self.lost = self.foreign = 0
        self.restarts = self.dup_or_back = 0
        self.first = self.last = None
        self.by_pattern = {}

    def add(self, frame):
        r = tp.check(frame.start_code, frame.slots)
        if r is None:
            self.foreign += 1
            return
        pattern, counter, ok = r
        if not ok:
            # Don't trust the counter of a corrupt packet.
            self.corrupt += 1
            return
        self.ok += 1
        self.by_pattern[pattern] = self.by_pattern.get(pattern, 0) + 1
        if self.last is None:
            self.first = counter
        elif counter > self.last:
            self.lost += counter - self.last - 1
        elif counter < self.last - 1000:
            self.restarts += 1          # ESP32 reset; counter starts again at 0
        else:
            self.dup_or_back += 1
        self.last = counter


class Esp32Link:
    """USB serial to the txtest firmware: send pattern commands, read status."""

    def __init__(self, port):
        import serial
        self.ser = serial.Serial()
        self.ser.port = port
        self.ser.baudrate = 115200
        self.ser.timeout = 0
        self.ser.dtr = False   # don't reset the DevKit on open
        self.ser.rts = False
        self.ser.open()
        self.buf = b""
        self.status = None     # (pattern, next_counter, mode)
        self.status_time = None
        self.messages = []

    def command(self, cmd):
        self.ser.write(cmd.encode() + b"\n")

    def wait_for_pattern(self, pattern, baud=0, timeout=10):
        # If opening the port reset the board, it reboots in ~0.5 s; status lines
        # buffered before that are stale, so wait it out and start clean.
        time.sleep(1.5)
        self.ser.reset_input_buffer()
        self.buf, self.status, self.messages = b"", None, []
        want_mode = "cycle" if pattern == "cycle" else "pin"
        cmd = "c" if pattern == "cycle" else f"p{pattern}"
        deadline = time.monotonic() + timeout
        last_cmd = 0
        while time.monotonic() < deadline:
            self.poll()
            st = self.status
            if (st and st[2] == want_mode and st[3] == baud
                    and (pattern == "cycle" or st[0] == int(pattern))):
                time.sleep(0.5)   # let the new pattern settle on the wire
                self.poll()
                self.messages.clear()
                return
            if time.monotonic() - last_cmd > 1:
                self.command(f"b{baud}")
                self.command(cmd)  # resend until the firmware confirms it
                last_cmd = time.monotonic()
            time.sleep(0.05)
        raise SystemExit(f"ESP32 on {self.ser.port} didn't confirm pattern {pattern!r}; "
                         f"last output: {self.messages[-3:]}")

    def poll(self):
        self.buf += self.ser.read(4096)
        *lines, self.buf = self.buf.split(b"\n")
        for raw in lines:
            line = raw.decode(errors="replace").strip()
            m = re.match(r"S (\d+) (\d+) (\w+)(?: baud=(\d+))?", line)
            if m:
                self.status = (int(m[1]), int(m[2]), m[3], int(m[4] or 0))
                self.status_time = time.monotonic()
            elif line:
                self.messages.append(line)


def fmt_sc(counter):
    return ",".join(f"{sc:02X}:{n}" for sc, n in sorted(counter.items())) or "-"


def run_live(args):
    line_no = int(re.search(r"(\d+)$", args.port)[1])
    esp = None
    if args.crosscheck:
        # Opening the USB port can reset the ESP32 (DTR pulses on open), and GPIO14
        # glitches while it boots, so set the pattern and wait for it to be
        # running before the DMX port is opened and anything is counted.
        esp = Esp32Link(args.crosscheck)
        esp.wait_for_pattern(args.pattern, args.baud)
    fd = open_dmx_port(args.port)
    if args.rt:
        os.sched_setscheduler(0, os.SCHED_FIFO, os.sched_param(args.rt))
    parser = DmxParser()
    rec = open(args.record, "wb") if args.record else None
    verify = Verifier() if (args.crosscheck or args.verify) else None

    # Warm-up: opening the port mid-stream can catch half a byte (one framing
    # error) before the first BREAK, so run for a second, then start counting.
    warm_end = time.monotonic() + 1.0
    while time.monotonic() < warm_end:
        if select.select([fd], [], [], 0.05)[0]:
            for f in parser.feed(os.read(fd, 65536)):
                if verify:
                    verify.add(f)
    if esp:
        esp.poll()
        esp.messages.clear()
    parser.stats = ParserStats()
    if verify:
        verify = Verifier()

    k0 = kernel_counters(line_no)
    if k0 is None:
        print(f"note: can't read {PROC_TTY} (run under sudo for oe/fe/brk)", file=sys.stderr)
    kprev = k0
    s = parser.stats
    prev = dict(frames=0, other=0, malformed=0, err=0, ok=0, lost=0, corrupt=0)
    slot_min = slot_max = None
    start = last_print = time.monotonic()
    max_read = 0
    print("time  fps  other  slots    malf  errb  | k_oe k_fe k_brk"
          + ("  | ok/s  lost corrupt" if verify else ""))
    try:
        while True:
            now = time.monotonic()
            if args.duration and now - start >= args.duration:
                break
            r, _, _ = select.select([fd], [], [], max(0.0, last_print + 1 - now))
            if r:
                data = os.read(fd, 65536)
                max_read = max(max_read, len(data))
                if rec:
                    rec.write(data)
                for f in parser.feed(data):
                    n = len(f.slots)
                    slot_min = n if slot_min is None else min(slot_min, n)
                    slot_max = n if slot_max is None else max(slot_max, n)
                    if verify:
                        verify.add(f)
            if esp:
                esp.poll()
            now = time.monotonic()
            if now - last_print >= 1:
                last_print = now
                other = sum(s.other_start_codes.values())
                k = kernel_counters(line_no)
                kd = {key: (k[key] - kprev[key]) & 0xFFFFFFFF for key in k} if k and kprev else None
                kprev = k
                stamp = time.strftime("%H:%M:%S") if args.wallclock else f"{now - start:5.0f}"
                line = (f"{stamp} {s.frames - prev['frames']:4d} "
                        f"{other - prev['other']:5d}  {slot_min or 0:3d}-{slot_max or 0:<3d} "
                        f"{s.malformed - prev['malformed']:5d} {s.error_bytes - prev['err']:5d}  | ")
                line += (f"{kd['oe']:4d} {kd['fe']:4d} {kd['brk']:5d}" if kd else "   -    -     -")
                if verify:
                    line += (f"  | {verify.ok - prev['ok']:5d} {verify.lost - prev['lost']:5d} "
                             f"{verify.corrupt - prev['corrupt']:7d}")
                if esp and esp.status:
                    p, nxt, mode, baud = esp.status
                    line += f"  esp:{tp.NAMES[p] if p < len(tp.NAMES) else p}/{mode} next={nxt}"
                    if baud:
                        line += f" baud={baud}"
                print(line, flush=True)
                if esp:
                    for m in esp.messages:
                        print(f"      esp32: {m}")
                    esp.messages.clear()
                prev = dict(frames=s.frames, other=other, malformed=s.malformed,
                            err=s.error_bytes,
                            ok=verify.ok if verify else 0, lost=verify.lost if verify else 0,
                            corrupt=verify.corrupt if verify else 0)
                slot_min = slot_max = None
    except KeyboardInterrupt:
        pass
    finally:
        if rec:
            rec.close()

    # Catch the tail: frames the ESP32 says it sent that never showed up.
    tail_lost = 0
    if esp:
        esp.poll()
        deadline = time.monotonic() + 0.3
        while time.monotonic() < deadline:
            if select.select([fd], [], [], 0.05)[0]:
                for f in parser.feed(os.read(fd, 65536)):
                    verify.add(f)
        if esp.status and verify.last is not None:
            tail_lost = max(0, esp.status[1] - 1 - verify.last)
    os.close(fd)

    elapsed = time.monotonic() - start
    k = kernel_counters(line_no)
    print("\n=== summary ===")
    print(f"duration        {elapsed:.0f} s")
    print(f"frames (sc 00)  {s.frames}  ({s.frames / elapsed:.1f}/s)")
    print(f"other sc        {fmt_sc(s.other_start_codes)}")
    print(f"malformed       {s.malformed}  (overlong {s.overlong})")
    print(f"error bytes     {s.error_bytes}   bad escapes {s.bad_escapes}   empty {s.empty}")
    print(f"largest read    {max_read} bytes")
    failed = s.malformed > 0 or s.error_bytes > 0
    if k and k0:
        kd = {key: (k[key] - k0[key]) & 0xFFFFFFFF for key in k}
        print(f"kernel          oe {kd['oe']}  fe {kd['fe']}  brk {kd['brk']}  rx {kd['rx']}")
        failed |= kd["oe"] > 0
    if verify:
        print(f"verified ok     {verify.ok}   by pattern "
              + ", ".join(f"{tp.NAMES[p]}:{n}" for p, n in sorted(verify.by_pattern.items())))
        print(f"lost            {verify.lost} (+{tail_lost} at end)   corrupt {verify.corrupt}   "
              f"non-test {verify.foreign}   dup/back {verify.dup_or_back}   esp32 resets {verify.restarts}")
        if verify.first is not None:
            print(f"counter range   {verify.first} .. {verify.last}")
        failed |= verify.lost + tail_lost + verify.corrupt > 0 or verify.ok == 0
    print("RESULT          " + ("FAIL" if failed else "PASS"))
    return 1 if failed else 0


def run_selftest(args):
    failed = False
    rng = random.Random(1234)

    # Generated streams: every test pattern, random read sizes, including 1-byte reads.
    for pattern, name in enumerate(tp.NAMES):
        packets = [tp.packet(pattern, c) for c in range(500)]
        stream = b"".join(tp.encode_parmrk(sc, sl) for sc, sl in packets) + b"\xff\x00\x00"
        p, v, i = DmxParser(), Verifier(), 0
        while i < len(stream):
            n = rng.choice([1, 2, 3, rng.randrange(1, 5000)])
            for f in p.feed(stream[i:i + n]):
                v.add(f)
            i += n
        ok = v.ok == 500 and v.lost == 0 and v.corrupt == 0 and p.stats.malformed == 0
        failed |= not ok
        print(f"generated {name:11s} {'ok' if ok else 'FAIL'}  ({v.ok}/500 verified)")

    # Truncated stream: cut mid-packet, then carry on.
    sc, sl = tp.packet(tp.FULL, 1)
    whole = tp.encode_parmrk(sc, sl)
    stream = whole[:300] + whole + b"\xff\x00\x00"
    p = DmxParser()
    frames = p.feed(stream)
    ok = len(frames) == 2 and frames[1].slots == sl
    failed |= not ok
    print(f"truncated packet    {'ok' if ok else 'FAIL'}")

    for path in args.selftest:
        data = Path(path).read_bytes()
        p = DmxParser()
        v = Verifier()
        sizes = []
        for f in p.feed(data):
            sizes.append(len(f.slots))
            v.add(f)
        s = p.stats
        print(f"\n{path}: {len(data)} bytes")
        print(f"  frames {s.frames}  other sc {fmt_sc(s.other_start_codes)}  malformed {s.malformed}"
              f"  error bytes {s.error_bytes}  bad escapes {s.bad_escapes}  breaks {s.breaks}")
        if sizes:
            print(f"  slots min {min(sizes)}  max {max(sizes)}")
        if v.ok or v.corrupt:
            print(f"  test pattern: ok {v.ok}  lost {v.lost}  corrupt {v.corrupt}")
        # Replaying the same capture in 1-byte reads must give identical results.
        p1 = DmxParser()
        n1 = sum(len(p1.feed(data[i:i + 1])) for i in range(len(data)))
        same = n1 == len(sizes) and p1.stats == p.stats
        failed |= not same
        print(f"  1-byte replay {'identical' if same else 'DIFFERENT'}")

    print("RESULT " + ("FAIL" if failed else "PASS"))
    return 1 if failed else 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", default="/dev/ttyAMA3")
    ap.add_argument("--duration", type=float, default=0, help="seconds (0 = until Ctrl-C)")
    ap.add_argument("--record", metavar="FILE", help="save the raw PARMRK stream")
    ap.add_argument("--selftest", nargs="*", metavar="CAPTURE",
                    help="run the parser on generated streams and the given captures, then exit")
    ap.add_argument("--crosscheck", metavar="USB_PORT", help="ESP32 txtest USB serial, e.g. /dev/ttyUSB0")
    ap.add_argument("--pattern", default="cycle",
                    help=f"txtest pattern 0-{len(tp.NAMES) - 1} ({', '.join(tp.NAMES)}) or 'cycle'")
    ap.add_argument("--baud", type=int, default=0,
                    help="force the txtest baud for every pattern, e.g. 245000 (0 = pattern default)")
    ap.add_argument("--verify", action="store_true", help="check test patterns without the USB link")
    ap.add_argument("--wallclock", action="store_true", help="timestamp lines with the time of day")
    ap.add_argument("--rt", type=int, metavar="PRIO", help="run as SCHED_FIFO at this priority")
    args = ap.parse_args()
    if args.selftest is not None:
        return run_selftest(args)
    return run_live(args)


if __name__ == "__main__":
    sys.exit(main())
