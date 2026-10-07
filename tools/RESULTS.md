# Phase 1 results: Pi-only DMX input

Pass: 0 kernel `oe`, 0 malformed, every sent frame received intact (crosscheck),
for the ESP32 patterns and the dongle; `cyclictest` max well under 700 µs.

## Setup
- Pi 4, kernel `6.6.51+rpt-rpi-v8`, input `/dev/ttyAMA3` (GPIO5, pin 29)
- ESP32 backup: `~/esp32_backups/esp32-<mac>-<date>.bin` (sha256 alongside)
- Load = `sudo tools/load.sh` (SD writes, flood ping, CPU hogs on 3 cores)
- Before the runs, the kernel already showed `oe:28` on ttyAMA3 from the production ESP32 link at 921600 baud

## cyclictest
`sudo cyclictest -m -S -p 90 -i 200 -D 5m -q` (with and without load)

| Load | IRQ 36 | Max (µs) | Avg (µs) |
|---|---|---|---|
| idle | all CPUs | | |
| load | all CPUs | | |
| load | CPU3 | | |

## ESP32 txtest patterns
`sudo python3 tools/dmx_rx_probe.py --crosscheck /dev/ttyUSB0 --pattern N --duration 600`

| Pattern | Load | IRQ | fps | verified | lost | corrupt | malformed | k_oe | k_fe | Result |
|---|---|---|---|---|---|---|---|---|---|---|
| 0 full512 | idle | all | | | | | | | | |
| 0 full512 | load | all | | | | | | | | |
| 0 full512 | load | CPU3 | | | | | | | | |
| 1 short24 | idle | all | | | | | | | | |
| 1 short24 | load | all | | | | | | | | |
| 1 short24 | load | CPU3 | | | | | | | | |
| 2 startcodes | idle | all | | | | | | | | |
| 2 startcodes | load | all | | | | | | | | |
| 2 startcodes | load | CPU3 | | | | | | | | |
| 3 escapes | idle | all | | | | | | | | |
| 3 escapes | load | all | | | | | | | | |
| 3 escapes | load | CPU3 | | | | | | | | |
| 4 varlen | idle | all | | | | | | | | |
| 4 varlen | load | all | | | | | | | | |
| 4 varlen | load | CPU3 | | | | | | | | |

## Enttec Open DMX dongle (step 1.6), 2026-10-07
Enttec Open DMX USB (FT232R) driven from the Pi by `tools/opendmx_tx.py` (RTS cleared, 120 us
BREAK), 5-pin output to the box's 3-pin input by clip leads (1-1, 2-2, 3-3), shield MAX485 ->
RX0 -> 1 k / 2 k divider -> pin 29 (ttyAMA3). Probe with `--verify` (counter gaps + byte check).
Short runs only (18-20 s each); long dongle runs not done.

| Run | fps | Frames verified | Lost | Corrupt | Malformed | Error bytes | k_oe | k_fe | Result |
|---|---|---|---|---|---|---|---|---|---|
| full512 | 31.1 | 623 | 0 | 0 | 0 | 0 | 0 | 0 | PASS |
| startcodes (00/17/CF) | 31 | 559 | 0 | 0 | 0 | 0 | 0 | 0 | PASS |
| escapes (0x11/0x13/0xFF) | 31.9 | 575 | 0 | 0 | 0 | 0 | 0 | 0 | PASS |
| varlen (5-512 slots) | 50.3 | 906 | 0 | 0 | 0 | 0 | 0 | 0 | PASS |
| fade (ch 17-19 = 11/13/FF) | 31.2 | n/a | n/a | n/a | 0 | 0 | 0 | 0 | PASS |
| full512 under load | 28.4 | 512 | 0 | 0 | 0 | 0 | 0 | 0 | PASS |

The first attempt saw nothing on pin 29: the clip leads were on the wrong XLR pins. The 3-pin
and 5-pin faces differ (pins 1 and 2 are side by side on top of a 3-pin, on the right side of
a 5-pin); only pin 3 sits in the same place.

## Quick suite, 2026-10-06 19:20 (18 s per run)
`sudo tools/phase1_suite.sh 18`, ESP32 on pin 29. All 22 runs PASS: patterns 0-5 idle, under load,
under load with IRQ 36 on CPU3, plus full512 and timing at 245k and 255k baud under load.
64,277 frames verified, 0 lost, 0 corrupt, 0 kernel oe/fe. cyclictest (20 s) under load max 241 us.

## Overnight soak, 2026-10-06 22:11 to 2026-10-07 08:11
`tools/overnight.sh 10`, ESP32 GPIO14 -> pin 29 (ttyAMA3), GPIO5 pull-up on, IRQ unpinned.
20 x 30 min blocks cycling full512 idle / full512 load / timing load / timing idle.
Logs: `~/phase1_logs/overnight-20261006-2211/`.

| Blocks | Frames verified | Lost | Corrupt | Malformed | k_oe | k_fe | Result |
|---|---|---|---|---|---|---|---|
| 20 / 20 | 1,895,113 | 0 | 0 | 0 | 0 | 0 | PASS |

Every one of the 35,851 per-second lines was clean. The ttyAMA3 lifetime oe counter stayed at 28
(all from the old 921600-baud ESP32 link before testing began).

## Notes
- The 28 oe on the old link: at 921600 baud the PL011 has ~174 us after its half-full RX interrupt
  before overrunning; at 250k 8N2 it has ~704 us. Idle cyclictest max was 205 us.
- Open: ESP32 on pin 10 (GPIO15, ttyAMA0) corrupts bits (0 read as 1) only while GPIO15's pull-up
  is on; clean with no pull. Wire 0 ohm, static levels 0.02 / 3.32 V. Same wire on pin 29 is clean
  with the pull-up. Needs a scope, or a UART5 (pin 33) comparison.
- Step 1.6 done in short runs (above). Remaining: the ColorSource capture at the venue (phase 5).
