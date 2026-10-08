import os
import pty
import time

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
        time.sleep(0.3)
        for fill in (5, 6, 7):
            os.write(master, esp32_frame(fill))
            time.sleep(0.05)
        assert wait_for(lambda: r.snapshot()[2][:3] == bytes([7, 7, 7]))
        assert r.stats()["frames"] >= 3 or wait_for(lambda: r.stats()["frames"] >= 3)
    finally:
        r.stop()
        os.close(master)
        os.close(slave)
