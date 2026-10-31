"""The recorder keeps enough to rebuild exactly what the console sent."""

import asyncio
import random

from engine import recorder
from engine.recorder import Recorder, dmx_frames


class StubEngine:
    cfg = {k: {} for k in recorder.CONFIG_SECTIONS}

    def status(self):
        return {}


def frames(n, seed=1):
    """A fade on a few channels, a slow keyframe gap, and full-rig jumps."""
    rng = random.Random(seed)
    data = bytearray(512)
    out = []
    t = 100.0
    for i in range(n):
        t += 0.025
        if i % 50 == 49:
            data = bytearray(rng.randrange(256) for _ in range(512))    # every channel changes
        elif i % 3 == 0:
            for ch in (0, 1, 2, 511):
                data[ch] = (data[ch] + 1) % 256
        out.append((i + 1, t, bytes(data)))
    return out


def test_recording_rebuilds_every_change_with_its_timing(tmp_path):
    async def go():
        rec = Recorder(StubEngine(), tmp_path)
        rec.start()
        rec._t0 = 100.0
        sent = frames(600)
        for seq, t, data in sent:
            rec.on_frame(seq, t, data)
        rec.stop()
        await asyncio.to_thread(rec.join)
        return rec
    rec = asyncio.run(go())
    rebuilt = list(dmx_frames(rec.dir / rec.list()[0]["name"]))
    # Each frame that differs from the one before it, plus the first and any keyframes.
    by_seq = {seq: (t, data) for seq, t, data in frames(600)}
    state = None
    seen = {seq: data for _, seq, data in rebuilt}
    for seq, (t, data) in by_seq.items():
        if data != state:
            assert seen[seq] == data, f"frame {seq} wasn't kept"
        state = data
    for t, seq, data in rebuilt:
        assert abs(t - (by_seq[seq][0] - 100.0)) < 0.001 and data == by_seq[seq][1]


def test_unchanged_frames_add_nothing_until_a_keyframe(tmp_path):
    async def go():
        rec = Recorder(StubEngine(), tmp_path)
        rec.start()
        data = bytes(512)
        for i in range(100):
            rec.on_frame(i + 1, rec._t0 + i * 0.025, data)
        rec.stop()
        await asyncio.to_thread(rec.join)
        return rec
    rec = asyncio.run(go())
    assert len(list(dmx_frames(rec.dir / rec.list()[0]["name"]))) == 1


def test_a_line_cut_off_by_a_power_failure_is_skipped(tmp_path):
    path = tmp_path / "recording-1.jsonl"
    path.write_text('{"type":"dmx","seq":1,"t":0.1,"values":"' + "00" * 512 + '"}\n'
                    '{"type":"dmx","seq":2,"t":0.2,"changes":[1,255]}\n'
                    '{"type":"dmx","seq":3,"t":0.3,"chan')
    got = list(dmx_frames(path))
    assert [g[1] for g in got] == [1, 2] and got[1][2][0] == 255
