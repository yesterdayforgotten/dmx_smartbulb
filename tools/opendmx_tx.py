#!/usr/bin/env python3
"""Drive an Enttec Open DMX USB (FTDI, no microcontroller) from the Pi.

The host makes the DMX timing: 250000 baud 8N2, a BREAK from the FTDI's line
break control, then the packet. Timing between packets is loose (USB + Python),
much like QLC+ driving the same dongle.

    python3 tools/opendmx_tx.py --pattern 0          # self-verifying test packets
    python3 tools/opendmx_tx.py --fade               # 512 channels fading through 0x11/0x13/0xFF
    python3 tools/opendmx_tx.py --pattern 0 --profile console --rdm-every 40

--profile console varies line timing per packet like a real (and sloppy) console:
baud at the FT232R's steps nearest +-2%, BREAK 88 us-3 ms, longer MABs, 0-30 ms
between packets, and some packets split into chunks with 1-3 ms gaps mid-packet.
--rdm-every N sends RDM traffic every N packets: a discovery request (start code
0xCC) followed by a discovery response with no BREAK, as responders send them,
or a GET request/response pair.

Check reception on the Pi with:
    sudo python3 tools/dmx_rx_probe.py --verify      # for --pattern
    sudo python3 tools/dmx_rx_probe.py               # for --fade
"""

import argparse
import random
import sys
import time
from pathlib import Path

import serial

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools import dmx_testpattern as tp  # noqa: E402

SLOT_BITS = 11  # start + 8 data + 2 stop
# FT232R baud rates are 3 MHz / (n + k/8); these are its steps nearest 250k +-2%.
CONSOLE_BAUDS = (244898, 247423, 250000, 252632, 255319)
CONTROLLER_UID = bytes.fromhex("454E0000BEEF")
RESPONDER_UID = bytes.fromhex("02A1C0FFEE01")


def rdm_packet(cc, pid, pdl=b"", dest=b"\xff" * 6, src=CONTROLLER_UID, tn=0):
    """An RDM packet (without the 0xCC start code) with its checksum."""
    body = bytes([0x01, 24 + len(pdl)]) + dest + src + bytes([tn, 1, 0, 0, 0, cc]) \
        + pid.to_bytes(2, "big") + bytes([len(pdl)]) + pdl
    total = (0xCC + sum(body)) & 0xFFFF
    return body + total.to_bytes(2, "big")


def dub_response(uid):
    """Discovery response as sent on the wire: preamble, separator, then the UID
    and checksum with each byte encoded as (b | 0xAA, b | 0x55). No BREAK."""
    enc = bytearray()
    for b in uid:
        enc += bytes([b | 0xAA, b | 0x55])
    csum = sum(enc) & 0xFFFF
    for b in csum.to_bytes(2, "big"):
        enc += bytes([b | 0xAA, b | 0x55])
    return b"\xfe" * 7 + b"\xaa" + bytes(enc)


def fade_packet(t):
    """512 channels; a slow sweep plus channels parked on 0x11, 0x13 and 0xFF."""
    v = int(t * 64) & 0xFF
    slots = bytearray((v + i) & 0xFF for i in range(512))
    slots[16:19] = bytes([0x11, 0x13, 0xFF])  # channels 17, 18, 19
    return 0, bytes(slots)


def send_packet(ser, data, baud, break_us, mab_us, chunks, rng, with_break=True):
    """BREAK, MAB, then data (optionally in chunks with 1-3 ms gaps). Returns once
    the last byte has surely left the FTDI chip."""
    if ser.baudrate != baud:
        ser.baudrate = baud
    slot = SLOT_BITS / baud
    if with_break:
        ser.break_condition = True
        time.sleep(break_us / 1e6)
        ser.break_condition = False
        time.sleep(mab_us / 1e6)
    cuts = sorted(rng.sample(range(1, len(data)), chunks - 1)) if chunks > 1 and len(data) > chunks else []
    pieces = [data[a:b] for a, b in zip([0] + cuts, cuts + [len(data)])]
    for i, piece in enumerate(pieces):
        t_write = time.monotonic()
        ser.write(piece)
        ser.flush()
        # flush() only empties the kernel buffer; the FTDI chip can still hold
        # up to 256 bytes. Wait until the piece has surely left the chip, or the
        # next BREAK cuts it short. The 3 ms margin covers USB delays under load.
        gap = rng.uniform(0.001, 0.003) if i < len(pieces) - 1 else 0
        remaining = len(piece) * slot + 0.003 + gap - (time.monotonic() - t_write)
        if remaining > 0:
            time.sleep(remaining)


def send_rdm(ser, rng, counter):
    """RDM traffic: a discovery request answered by a BREAK-less response, or a
    GET DEVICE_INFO request and its (normal, BREAK-led) response."""
    if rng.random() < 0.5:
        req = rdm_packet(0x10, 0x0001, b"\x00" * 6 + b"\xff" * 6, tn=counter & 0xFF)
        send_packet(ser, b"\xcc" + req, 250000, 176, 12, 1, rng)
        time.sleep(rng.uniform(0.001, 0.002))
        send_packet(ser, dub_response(RESPONDER_UID), 250000, 0, 0, 1, rng, with_break=False)
    else:
        req = rdm_packet(0x20, 0x0060, dest=RESPONDER_UID, tn=counter & 0xFF)
        send_packet(ser, b"\xcc" + req, 250000, 176, 12, 1, rng)
        time.sleep(rng.uniform(0.001, 0.002))
        info = bytes(19)
        resp = rdm_packet(0x21, 0x0060, info, dest=CONTROLLER_UID, src=RESPONDER_UID, tn=counter & 0xFF)
        send_packet(ser, b"\xcc" + resp, 250000, 176, 12, 1, rng)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", default="/dev/ttyUSB1")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--pattern", type=int, choices=range(len(tp.NAMES)),
                      help="test pattern (" + ", ".join(f"{i} {n}" for i, n in enumerate(tp.NAMES)) + ")")
    mode.add_argument("--fade", action="store_true")
    ap.add_argument("--rate", type=float, default=0, help="packets/s (0 = as fast as the dongle allows)")
    ap.add_argument("--duration", type=float, default=0, help="seconds (0 = until Ctrl-C)")
    ap.add_argument("--profile", choices=("fixed", "console"), default="fixed")
    ap.add_argument("--rdm-every", type=int, default=0, metavar="N", help="RDM traffic every N packets")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--break-us", type=int, default=120)
    ap.add_argument("--mab-us", type=int, default=20)
    args = ap.parse_args()

    ser = serial.Serial(args.port, baudrate=250000, bytesize=8, parity="N", stopbits=2,
                        xonxoff=False, rtscts=False, write_timeout=1)
    # Like OLA: the Open DMX needs RTS cleared (on many units it gates the line driver).
    ser.rts = False
    rng = random.Random(args.seed)
    counter = 0
    rdm_sent = 0
    start = last_report = time.monotonic()
    sent_this_second = 0
    try:
        while not args.duration or time.monotonic() - start < args.duration:
            t0 = time.monotonic()
            if args.fade:
                sc, slots = fade_packet(t0 - start)
            else:
                sc, slots = tp.packet(args.pattern, counter)
            data = bytes([sc]) + slots

            if args.profile == "console":
                baud = rng.choice(CONSOLE_BAUDS)
                brk = rng.randint(100, 500) if rng.random() < 0.8 else rng.randint(88, 3000)
                mab = rng.randint(12, 100) if rng.random() < 0.8 else rng.randint(100, 1000)
                chunks = rng.choice((1, 1, 1, 2, 3))
                idle = rng.uniform(0, 0.030)
            else:
                baud, brk, mab, chunks, idle = 250000, args.break_us, args.mab_us, 1, 0
            send_packet(ser, data, baud, brk, mab, chunks, rng)
            if idle:
                time.sleep(idle)

            if args.rdm_every and counter % args.rdm_every == args.rdm_every - 1:
                send_rdm(ser, rng, counter)
                rdm_sent += 1
            if args.rate:
                wait = 1 / args.rate - (time.monotonic() - t0)
                if wait > 0:
                    time.sleep(wait)

            counter += 1
            sent_this_second += 1
            now = time.monotonic()
            if now - last_report >= 1:
                print(f"{now - start:5.0f} s  {sent_this_second} packets/s  sent {counter}", flush=True)
                sent_this_second = 0
                last_report = now
    except KeyboardInterrupt:
        pass
    finally:
        ser.close()
    print(f"sent {counter} packets" + ("" if args.fade else f" (counter 0..{counter - 1})")
          + (f", {rdm_sent} RDM exchanges" if rdm_sent else ""))


if __name__ == "__main__":
    main()
