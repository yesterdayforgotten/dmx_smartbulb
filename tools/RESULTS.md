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

## Enttec Open DMX dongle (step 1.6)
QLC+ at maximum frequency, 512 channels, fades through 17, 19, 255.
`sudo python3 tools/dmx_rx_probe.py --duration 600 --record dongle.bin`

| Load | fps | slots | malformed | error bytes | k_oe | k_fe | Result |
|---|---|---|---|---|---|---|---|
| idle | | | | | | | |
| load | | | | | | | |

## Notes
