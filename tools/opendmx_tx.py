#!/usr/bin/env python3
"""Drive an Enttec Open DMX USB (FTDI, no microcontroller) from the Pi.

The host makes the DMX timing: 250000 baud 8N2, a BREAK from the FTDI's line
break control, then the packet. Timing between packets is loose (USB + Python),
much like QLC+ driving the same dongle.

    python3 tools/opendmx_tx.py --pattern 0          # self-verifying test packets
    python3 tools/opendmx_tx.py --fade               # 512 channels fading through 0x11/0x13/0xFF

Check reception on the Pi with:
    sudo python3 tools/dmx_rx_probe.py --verify      # for --pattern
    sudo python3 tools/dmx_rx_probe.py               # for --fade
"""

import argparse
import sys
import time
from pathlib import Path

import serial

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools import dmx_testpattern as tp  # noqa: E402

SLOT_US = 44  # 11 bits at 4 us


def fade_packet(t):
    """512 channels; a slow sweep plus channels parked on 0x11, 0x13 and 0xFF."""
    v = int(t * 64) & 0xFF
    slots = bytearray((v + i) & 0xFF for i in range(512))
    slots[16:19] = bytes([0x11, 0x13, 0xFF])  # channels 17, 18, 19
    return 0, bytes(slots)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", default="/dev/ttyUSB1")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--pattern", type=int, choices=range(len(tp.NAMES)),
                      help="test pattern (" + ", ".join(f"{i} {n}" for i, n in enumerate(tp.NAMES)) + ")")
    mode.add_argument("--fade", action="store_true")
    ap.add_argument("--rate", type=float, default=0, help="packets/s (0 = as fast as the dongle allows)")
    ap.add_argument("--duration", type=float, default=0, help="seconds (0 = until Ctrl-C)")
    ap.add_argument("--break-us", type=int, default=120)
    ap.add_argument("--mab-us", type=int, default=20)
    args = ap.parse_args()

    ser = serial.Serial(args.port, baudrate=250000, bytesize=8, parity="N", stopbits=2,
                        xonxoff=False, rtscts=False, write_timeout=1)
    counter = 0
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

            ser.break_condition = True
            time.sleep(args.break_us / 1e6)
            ser.break_condition = False
            time.sleep(args.mab_us / 1e6)
            ser.write(data)
            ser.flush()
            # flush() only empties the kernel buffer; wait for the FTDI chip to
            # finish sending before the next BREAK, or the packet gets cut short.
            on_wire = len(data) * SLOT_US / 1e6
            remaining = on_wire + 0.001 - (time.monotonic() - t0)
            if remaining > 0:
                time.sleep(remaining)
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
    print(f"sent {counter} packets" + ("" if args.fade else f" (counter 0..{counter - 1})"))


if __name__ == "__main__":
    main()
