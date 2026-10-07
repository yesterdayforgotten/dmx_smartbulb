#!/bin/bash
# Background load for the phase 1 "under load" runs: SD card writes, network
# (flood ping + bulb-shaped UDP, see net_load.py) and CPU hogs. Runs until Ctrl-C (or for N seconds: tools/load.sh 600).
#   sudo tools/load.sh [seconds]
# Network load is a flood ping of the default gateway (needs root).
set -uo pipefail
DUR=${1:-0}
SCRATCH=${SCRATCH:-/var/tmp/dmx_load}
mkdir -p "$SCRATCH"
pids=()
cleanup() { kill "${pids[@]}" 2>/dev/null; wait 2>/dev/null; rm -rf "$SCRATCH"; echo "load stopped"; }
trap cleanup EXIT INT TERM

# SD writes: 32 MB files with fsync, over and over.
( while :; do dd if=/dev/zero of="$SCRATCH/sd" bs=1M count=32 conv=fsync status=none; rm -f "$SCRATCH/sd"; done ) &
pids+=($!)

# CPU: one busy loop per core, except one core left for everything else.
for _ in $(seq 1 $(( $(nproc) - 1 ))); do ( while :; do :; done ) & pids+=($!); done

# Network: flood ping the gateway with full-size packets.
GW=$(ip route | awk '/^default/ {print $3; exit}')
if [ -n "$GW" ] && [ "$(id -u)" = 0 ]; then
    ping -f -q -s 1400 "$GW" >/dev/null & pids+=($!)
    echo "flood-pinging $GW"
else
    echo "no network load (needs root and a default gateway)"
fi

# Bulb-shaped UDP traffic to the gateway's discard port (see net_load.py).
python3 "$(dirname "$0")/net_load.py" > /dev/null 2>&1 & pids+=($!)
echo "bulb-shaped UDP traffic running"

echo "load running (pids ${pids[*]})"
if [ "$DUR" -gt 0 ]; then sleep "$DUR"; else wait; fi
