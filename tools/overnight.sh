#!/bin/bash
# Long ESP32 txtest soak: 30-minute blocks cycling through full512 idle,
# full512 under load, timing under load, timing idle. Every block's per-second
# log is time-stamped so any error can be matched to the time and the load.
#   sudo tools/overnight.sh [hours] [port] [logdir]
# Run detached:
#   sudo systemd-run --unit dmx-overnight $PWD/tools/overnight.sh 10
set -uo pipefail
HOURS=${1:-10}
PORT=${2:-/dev/ttyAMA3}
LOG=${3:-/home/phola/phase1_logs/overnight-$(date +%Y%m%d-%H%M)}
BLOCK=${BLOCK:-1800}
HERE=$(cd "$(dirname "$0")" && pwd)
ESP=/dev/serial/by-id/usb-Silicon_Labs_CP2102_USB_to_UART_Bridge_Controller_0001-if00-port0
mkdir -p "$LOG"
SUMMARY=$LOG/summary.txt
load_pid=
start_load() { "$HERE/load.sh" > "$LOG/load.log" 2>&1 & load_pid=$!; sleep 3; }
stop_load()  { [ -n "$load_pid" ] && kill "$load_pid" 2>/dev/null; wait "$load_pid" 2>/dev/null; load_pid=; sleep 3; }
trap stop_load EXIT

{
    echo "overnight soak, $HOURS h in ${BLOCK}s blocks, port $PORT, started $(date)"
    echo "pin: $(pinctrl get 5 2>/dev/null)"
    "$HERE/uart_irq.sh" | head -1
    uname -r
} | tee "$SUMMARY"

end=$(( $(date +%s) + ${TOTAL_SECS:-$((HOURS * 3600))} ))  # TOTAL_SECS: short test runs
n=0
plan=("0 idle" "0 load" "5 load" "5 idle")
while [ "$(date +%s)" -lt "$end" ]; do
    set -- ${plan[$((n % 4))]}
    pattern=$1 cond=$2
    secs=$(( end - $(date +%s) )); [ "$secs" -gt "$BLOCK" ] && secs=$BLOCK
    [ "$secs" -lt 20 ] && break
    name=$(printf '%02d-p%s-%s' "$n" "$pattern" "$cond")
    [ "$cond" = load ] && start_load
    echo "== $name $(date +%H:%M:%S)" | tee -a "$SUMMARY"
    python3 "$HERE/dmx_rx_probe.py" --port "$PORT" --crosscheck "$ESP" --pattern "$pattern" \
        --duration "$secs" --wallclock > "$LOG/$name.txt" 2>&1
    sed -n '/=== summary ===/,$p' "$LOG/$name.txt" | tail -n +2 >> "$SUMMARY"
    [ "$cond" = load ] && stop_load
    n=$((n + 1))
done

echo "== finished $(date)" | tee -a "$SUMMARY"
# One row per block: name, verified, lost, corrupt, malformed, kernel oe/fe, result
awk '/^== [0-9]/ {name=$2}
     /^malformed/ {mal=$2}
     /^kernel/ {oe=$3; fe=$5}
     /^verified ok/ {ok=$3}
     /^lost/ {lost=$2; cor=$7}
     /^RESULT/ {printf "%-16s ok %-9s lost %-5s corrupt %-5s malformed %-4s oe %-3s fe %-4s %s\n", name, ok, lost, cor, mal, oe, fe, $2}' \
    "$SUMMARY" | tee -a "$SUMMARY"
