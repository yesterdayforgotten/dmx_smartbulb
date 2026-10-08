import os
import pty
import time
from pathlib import Path

import pytest

from engine.dmx_esp32 import HEADER, Esp32Framer
from engine.receiver import GlitchGuard, Receiver
from tools import dmx_testpattern as tp


def test_glitch_guard_holds_one_odd_length_packet():
    g = GlitchGuard()
    a = bytes(512)
    assert g.accept(a) == a
    assert g.accept(bytes(7)) is None          # junk with a new length: held
    assert g.accept(a) == a                     # back to normal
    assert g.held == 1


def test_glitch_guard_follows_a_real_length_change():
    g = GlitchGuard()
    g.accept(bytes(512))
    assert g.accept(bytes(24)) is None          # first packet at the new length is held
    assert g.accept(bytes(24)) == bytes(24)     # the second is accepted
    assert g.accept(bytes(24)) == bytes(24)


def esp32_frame(fill):
    return HEADER + bytes([fill]) * 512


def test_esp32_framer_normal_and_split():
    f = Esp32Framer()
    stream = esp32_frame(1) + esp32_frame(2)
    out = []
    for i in range(0, len(stream), 100):
        out += f.feed(stream[i:i + 100])
    assert out == [bytes([1]) * 512, bytes([2]) * 512]


def test_esp32_framer_resyncs_and_stays_bounded():
    f = Esp32Framer()
    assert f.feed(b"garbage" * 1000) == []
    assert len(f.buf) < len(HEADER)             # never grows without limit
    assert f.feed(b"xx" + esp32_frame(9)) == [bytes([9]) * 512]
    assert f.resyncs >= 1


def wait_for(cond, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture
def recording(tmp_path):
    path = tmp_path / "rec.bin"
    stream = b"".join(tp.encode_parmrk(*tp.packet(tp.FULL, c)) for c in range(200)) + b"\xff\x00\x00"
    path.write_bytes(stream)
    return path


def test_replay_receiver_publishes_frames(recording):
    r = Receiver("replay", str(recording))
    r.start()
    try:
        assert wait_for(lambda: r.snapshot()[0] >= 5)
        seq, t, data = r.snapshot()
        r_ = tp.check(0, data)
        assert r_ is not None and r_[2], "published channels should be a whole test packet"
        assert time.monotonic() - t < 1.0
        assert os.read(r.wake_fd, 100)          # wake-up bytes arrive
        st = r.stats()
        assert st["alive"] and st["slots"] == 512
    finally:
        r.stop()


def test_receiver_restarts_after_crash(recording):
    r = Receiver("replay", str(recording))
    r.start()
    try:
        assert wait_for(lambda: r.snapshot()[0] > 0)
        r.proc.kill()
        r.proc.join(2)
        r._next_start = 0
        r.check()                               # notices and restarts
        assert r.restarts == 1
        seq = r.snapshot()[0]
        assert wait_for(lambda: r.snapshot()[0] > seq)
    finally:
        r.stop()


def test_esp32_receiver_over_a_pty():
    master, slave = pty.openpty()
    r = Receiver("esp32", os.ttyname(slave))
    r.start()
    try:
        # Keep writing until the receiver (which may start slowly on a busy Pi)
        # has the port open and publishes the frames.
        def frames_arrive():
            for fill in (5, 6, 7):
                os.write(master, esp32_frame(fill))
                time.sleep(0.02)
            return r.snapshot()[2][:3] == bytes([7, 7, 7])
        assert wait_for(frames_arrive, 10.0)
        # The frame counter (updated once a second) catches up with what was published.
        assert wait_for(lambda: r.stats()["frames"] >= r.snapshot()[0] > 0, 5.0)
    finally:
        r.stop()
        os.close(master)
        os.close(slave)


def test_receiver_ends_on_sigterm_even_if_parent_had_a_handler(recording):
    """uvicorn installs a SIGTERM handler that only sets a flag; the forked
    receiver must not inherit it."""
    import signal as sig
    old = sig.signal(sig.SIGTERM, lambda *a: None)
    try:
        r = Receiver("replay", str(recording))
        r.start()
        assert wait_for(lambda: r.snapshot()[0] > 0)
        r.proc.terminate()
        r.proc.join(3)
        assert not r.proc.is_alive()
    finally:
        sig.signal(sig.SIGTERM, old)


def test_receiver_dies_with_its_parent(recording, tmp_path):
    """If the engine is killed outright, its receiver must not linger."""
    import subprocess
    import sys
    code = f"""
import sys, time
sys.path.insert(0, {str(Path(__file__).resolve().parent.parent)!r})
from engine.receiver import Receiver
r = Receiver("replay", {str(recording)!r}); r.start()
print(r.proc.pid, flush=True)
time.sleep(60)
"""
    p = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    child = int(p.stdout.readline())
    p.kill()
    p.wait()
    def gone():
        try:
            os.kill(child, 0)
            with open(f"/proc/{child}/stat") as f:
                return f.read().split()[2] == "Z"
        except ProcessLookupError:
            return True
    assert wait_for(gone, 3.0)
