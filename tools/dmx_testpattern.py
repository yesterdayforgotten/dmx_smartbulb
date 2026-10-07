"""Self-verifying DMX test patterns, shared by dmx_rx_probe.py and the
dmx_esp32_txtest firmware (src/main.cpp must generate exactly the same packets).

Every packet is fully determined by (pattern, counter):

    slot 1       pattern id
    slots 2-5    counter, u32 big-endian (one counter across all patterns)
    slots 6..N   fill(pattern, counter, j), j = 0-based slot index (j >= 5)

so the probe can check each packet byte for byte and count gaps in the counter.
"""

FULL, SHORT, STARTCODES, ESCAPES, VARLEN, TIMING = range(6)
# TIMING also varies line timing per packet (baud, BREAK, MAB, gaps); see the firmware.
NAMES = ["full512", "short24", "startcodes", "escapes", "varlen", "timing"]
ESC_VALUES = (0xFF, 0x11, 0x13, 0x00)
HEADER_SLOTS = 5


def packet(pattern, counter):
    """Return (start_code, slots) for one test packet."""
    sc = 0
    if pattern == SHORT:
        n = 24
    elif pattern == VARLEN:
        n = HEADER_SLOTS + (counter * 37) % 508   # 5..512
    elif pattern == TIMING:
        n = 24 + (counter * 97) % 489             # 24..512
    else:
        n = 512
    if pattern == STARTCODES:
        sc = (0x00, 0x17, 0xCF)[counter % 3]

    slots = bytearray(n)
    slots[0] = pattern
    slots[1:5] = (counter & 0xFFFFFFFF).to_bytes(4, "big")
    for j in range(HEADER_SLOTS, n):
        if pattern == ESCAPES:
            slots[j] = ESC_VALUES[(counter + j) % 4]
        elif pattern == STARTCODES:
            slots[j] = (counter * 7 + j) & 0xFF
        elif pattern == TIMING:
            slots[j] = (counter * 3 + j) & 0xFF
        else:
            slots[j] = (counter + j) & 0xFF
    return sc, bytes(slots)


def check(start_code, slots):
    """Verify a received packet. Returns (pattern, counter, ok) or None if it
    isn't a test packet at all (too short or unknown pattern id)."""
    if len(slots) < HEADER_SLOTS or slots[0] >= len(NAMES):
        return None
    pattern = slots[0]
    counter = int.from_bytes(slots[1:5], "big")
    return pattern, counter, packet(pattern, counter) == (start_code, bytes(slots))


def encode_parmrk(start_code, slots):
    """Encode one packet as the kernel delivers it with PARMRK: BREAK marker,
    then the bytes with every 0xFF doubled."""
    out = bytearray(b"\xff\x00\x00")
    for b in bytes([start_code]) + bytes(slots):
        out += b"\xff\xff" if b == 0xFF else bytes([b])
    return bytes(out)
