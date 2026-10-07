#!/bin/bash
# Step 1.4: run every txtest pattern idle, under load, and under load with the
# UART IRQ pinned to CPU3, plus baud-skew runs (245k / 255k) and cyclictest.
# Needs the txtest firmware on the ESP32 and the dmx_smartbulb service stopped.
#   sudo tools/phase1_suite.sh [seconds_per_run] [logdir]
# 20 s per run takes about 9 minutes in total; 600 s per run about 3.5 hours.
set -uo pipefail
SECS=${1:-600}
LOG=${2:-/home/phola/phase1_logs/$(date +%Y%m%d-%H%M)}
HERE=$(cd "$(dirname "$0")" && pwd)
mkdir -p "$LOG"
SUMMARY=$LOG/summary.txt
load_pid=

start_load() { "$HERE/load.sh" > "$LOG/load.log" 2>&1 & load_pid=$!; sleep 3; }
stop_load()  { [ -n "$load_pid" ] && kill "$load_pid" 2>/dev/null; wait "$load_pid" 2>/dev/null; load_pid=; sleep 3; }
trap 'stop_load; "$HERE/uart_irq.sh" unpin >/dev/null' EXIT

cyc() {  # name
    echo "== cyclictest $1" | tee -a "$SUMMARY"
    cyclictest -m -S -p 90 -i 200 -D $(( SECS < 180 ? SECS : 180 )) -q > "$LOG/cyclictest-$1.txt" 2>&1
    grep "^T:" "$LOG/cyclictest-$1.txt" | tee -a "$SUMMARY"
}

run() {  # pattern condition [baud]
    local name=p$1-$2${3:+-$3}
    echo "== $name ($(date +%H:%M))" | tee -a "$SUMMARY"
    python3 "$HERE/dmx_rx_probe.py" --crosscheck /dev/ttyUSB0 --pattern "$1" \
        --baud "${3:-0}" --duration "$SECS" > "$LOG/$name.txt" 2>&1
    sed -n '/=== summary ===/,$p' "$LOG/$name.txt" | tail -n +2 >> "$SUMMARY"
}

"$HERE/uart_irq.sh" unpin >/dev/null
for p in 0 1 2 3 4 5; do run "$p" idle; done

start_load
cyc load-irq-all
for p in 0 1 2 3 4 5; do run "$p" load-irq-all; done
for b in 245000 255000; do run 0 load-irq-all "$b"; run 5 load-irq-all "$b"; done
"$HERE/uart_irq.sh" pin 3 >/dev/null
cyc load-irq-cpu3
for p in 0 1 2 3 4 5; do run "$p" load-irq-cpu3; done
stop_load
"$HERE/uart_irq.sh" unpin >/dev/null

echo "== done $(date +%H:%M)" | tee -a "$SUMMARY"
grep -E "^== p|^RESULT" "$SUMMARY" | paste - - | awk '{print $2, $NF}' | tee -a "$SUMMARY"
