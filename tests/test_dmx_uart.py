import random

import pytest

from engine.dmx_uart import DmxParser
from tools import dmx_testpattern as tp
from tools.dmx_testpattern import encode_parmrk

BREAK = b"\xff\x00\x00"


def feed_all(parser, data, chunk=None):
    frames = []
    if chunk is None:
        frames += parser.feed(data)
    else:
        for i in range(0, len(data), chunk):
            frames += parser.feed(data[i:i + chunk])
    return frames


def test_full_universe_emitted_without_waiting_for_next_break():
    p = DmxParser()
    slots = bytes(range(256)) * 2
    frames = p.feed(encode_parmrk(0, slots))
    assert len(frames) == 1
    assert frames[0].start_code == 0 and frames[0].slots == slots
    # The following BREAK must not emit it a second time.
    assert p.feed(BREAK) == []
    assert p.stats.frames == 1 and p.stats.malformed == 0


def test_short_frames_end_at_break():
    p = DmxParser()
    stream = encode_parmrk(0, b"\x01\x02\x03") + encode_parmrk(0, b"\x04") + BREAK
    frames = p.feed(stream)
    assert [f.slots for f in frames] == [b"\x01\x02\x03", b"\x04"]


def test_xon_xoff_and_ff_bytes_pass_through():
    p = DmxParser()
    slots = bytes([0x11, 0x13, 0xFF, 0xFF, 0x00, 0x11, 0xFF]) * 10
    frames = p.feed(encode_parmrk(0, slots) + BREAK)
    assert frames[0].slots == slots


@pytest.mark.parametrize("chunk", [1, 2, 3, 7, 64, 4095])
def test_escapes_split_across_reads(chunk):
    rng = random.Random(chunk)
    packets = [(0, bytes(rng.choice([0xFF, 0x00, 0x11, 0x13, rng.randrange(256)])
                         for _ in range(rng.randrange(1, 513))))
               for _ in range(50)]
    stream = b"".join(encode_parmrk(sc, s) for sc, s in packets) + BREAK
    p = DmxParser()
    frames = feed_all(p, stream, chunk)
    assert [(f.start_code, f.slots) for f in frames] == packets
    assert p.stats.malformed == 0 and p.stats.bad_escapes == 0


def test_data_before_first_break_is_discarded():
    p = DmxParser()
    frames = p.feed(b"\x05\x06\xff\xff\x07" + encode_parmrk(0, b"\x09") + BREAK)
    assert [f.slots for f in frames] == [b"\x09"]
    assert p.stats.sync_bytes == 4


def test_nonzero_start_codes_are_counted_and_returned():
    p = DmxParser()
    stream = (encode_parmrk(0x17, b"\x01") + encode_parmrk(0xCF, b"\x02")
              + encode_parmrk(0, b"\x03") + BREAK)
    frames = p.feed(stream)
    assert [f.start_code for f in frames] == [0x17, 0xCF, 0]
    assert p.stats.frames == 1
    assert p.stats.other_start_codes == {0x17: 1, 0xCF: 1}


def test_framing_error_byte_drops_packet():
    p = DmxParser()
    bad = BREAK + b"\x00\x01\xff\x00\x42\x03"
    frames = p.feed(bad + encode_parmrk(0, b"\x07") + BREAK)
    assert [f.slots for f in frames] == [b"\x07"]
    assert p.stats.error_bytes == 1 and p.stats.malformed == 1


def test_framing_error_in_full_universe_is_not_emitted_early():
    p = DmxParser()
    slots = bytearray(512)
    data = bytearray(encode_parmrk(0, slots))
    data[10:11] = b"\xff\x00\x55"  # replace one slot byte by an error marker
    assert p.feed(bytes(data)) == []
    assert p.feed(BREAK) == []
    assert p.stats.malformed == 1


def test_overlong_packet_is_counted():
    p = DmxParser()
    frames = p.feed(encode_parmrk(0, bytes(520)) + BREAK)
    # The first 513 bytes were already delivered; the overrun is still reported.
    assert len(frames) == 1
    assert p.stats.overlong == 1 and p.stats.malformed == 1


def test_truncated_packet_is_just_short():
    # A packet cut off by the next BREAK is a valid shorter packet in DMX.
    p = DmxParser()
    frames = p.feed(encode_parmrk(0, bytes(512))[:200] + encode_parmrk(0, b"\x01") + BREAK)
    assert len(frames[0].slots) < 512 and frames[1].slots == b"\x01"


def test_back_to_back_breaks_are_empty_not_malformed():
    p = DmxParser()
    p.feed(BREAK + BREAK + BREAK)
    assert p.stats.empty == 2 and p.stats.malformed == 0


def test_bad_escape_keeps_bytes():
    p = DmxParser()
    frames = p.feed(BREAK + b"\x00\x01\xff\x02" + BREAK)
    assert frames[0].slots == b"\x01\xff\x02"
    assert p.stats.bad_escapes == 1


def test_garbage_does_not_crash_and_resyncs():
    rng = random.Random(1)
    garbage = bytes(rng.randrange(256) for _ in range(20000))
    p = DmxParser()
    feed_all(p, garbage, 97)
    frames = p.feed(encode_parmrk(0, b"\x01\x02") + encode_parmrk(0, b"\x03") + BREAK)
    assert frames[-1].slots == b"\x03"


@pytest.mark.parametrize("pattern", range(len(tp.NAMES)))
def test_test_patterns_roundtrip(pattern):
    packets = [tp.packet(pattern, c) for c in range(1000, 1030)]
    stream = b"".join(encode_parmrk(sc, s) for sc, s in packets) + BREAK
    frames = feed_all(DmxParser(), stream, 333)
    assert len(frames) == len(packets)
    for f, c in zip(frames, range(1000, 1030)):
        assert tp.check(f.start_code, f.slots) == (pattern, c, True)


def test_check_detects_corruption():
    sc, slots = tp.packet(tp.FULL, 5)
    bad = bytearray(slots)
    bad[300] ^= 1
    assert tp.check(sc, bytes(bad)) == (tp.FULL, 5, False)
    assert tp.check(0x17, slots) == (tp.FULL, 5, False)
