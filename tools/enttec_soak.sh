#!/bin/bash
# Long soak of the real input path: Enttec Open DMX (sent from this Pi) ->
# shield -> divider -> pin 29. 30-minute blocks alternating idle / load (CPU, SD,
# flood ping, bulb-shaped UDP) across full512, escapes, varlen and startcodes,
# with fixed timing, console-style timing (+-2% baud, varied BREAK/MAB/idle,
# chunked packets) and RDM traffic (incl. BREAK-less discovery responses).
#   sudo tools/enttec_soak.sh [hours] [logdir]
# Run detached:
#   sudo systemd-run --unit dmx-enttec-soak $PWD/tools/enttec_soak.sh 4
set -uo pipefail
HOURS=${1:-4}
LOG=${2:-/home/phola/phase1_logs/enttec-soak-$(date +%Y%m%d-%H%M)}
BLOCK=${BLOCK:-1800}
PORT=${PORT:-/dev/ttyAMA3}
HERE=$(cd "$(dirname "$0")" && pwd)
ENTTEC=/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_B002N5CZ-if00-port0
mkdir -p "$LOG"
SUMMARY=$LOG/summary.txt
load_pid= tx_pid=
start_load() { "$HERE/load.sh" > "$LOG/load.log" 2>&1 & load_pid=$!; sleep 3; }
stop_load()  { [ -n "$load_pid" ] && kill "$load_pid" 2>/dev/null; wait "$load_pid" 2>/dev/null; load_pid=; sleep 3; }
stop_tx()    { [ -n "$tx_pid" ] && kill "$tx_pid" 2>/dev/null; wait "$tx_pid" 2>/dev/null; tx_pid=; }
trap 'stop_tx; stop_load' EXIT

{
    echo "enttec soak, $HOURS h in ${BLOCK}s blocks, port $PORT, started $(date)"
    echo "pin: $(pinctrl get 5 2>/dev/null)"
    "$HERE/uart_irq.sh" | head -1
} | tee "$SUMMARY"

end=$(( $(date +%s) + ${TOTAL_SECS:-$((HOURS * 3600))} ))  # TOTAL_SECS: short test runs
# pattern condition sender-options
plan=("0 idle fixed"
      "0 load fixed"
      "0 load console"
      "0 idle console"
      "3 idle console+rdm"
      "3 load console+rdm"
      "4 load console+rdm"
      "2 idle rdm")
n=${START_BLOCK:-0}
while [ "$(date +%s)" -lt "$end" ]; do
    set -- ${plan[$((n % 8))]}
    pattern=$1 cond=$2 mode=$3
    opts=()
    case $mode in *console*) opts+=(--profile console) ;; esac
    case $mode in *rdm*) opts+=(--rdm-every 40) ;; esac
    secs=$(( end - $(date +%s) )); [ "$secs" -gt "$BLOCK" ] && secs=$BLOCK
    [ "$secs" -lt 20 ] && break
    name=$(printf '%02d-p%s-%s-%s' "$n" "$pattern" "$cond" "$mode")
    [ "$cond" = load ] && start_load
    echo "== $name $(date +%H:%M:%S)" | tee -a "$SUMMARY"
    python3 "$HERE/opendmx_tx.py" --port "$ENTTEC" --pattern "$pattern" "${opts[@]}" --seed "$n" \
        > "$LOG/$name.tx.txt" 2>&1 &
    tx_pid=$!
    sleep 2
    python3 "$HERE/dmx_rx_probe.py" --port "$PORT" --verify --duration "$secs" --wallclock --max-silence 5 \
        > "$LOG/$name.txt" 2>&1
    stop_tx
    sed -n '/=== summary ===/,$p' "$LOG/$name.txt" | tail -n +2 >> "$SUMMARY"
    [ "$cond" = load ] && stop_load
    n=$((n + 1))
done

echo "== finished $(date)" | tee -a "$SUMMARY"
awk '/^== [0-9]/ {name=$2}
     /^malformed/ {mal=$2}
     /^error bytes/ {eb=$3}
     /^kernel/ {oe=$3; fe=$5}
     /^verified ok/ {ok=$3}
     /^lost/ {lost=$2; cor=$7; tr=$9; gsub(/\)/, "", tr)}
     /^silent seconds/ {sil=$6}
     /^RESULT/ {printf "%-28s silence %-4s ok %-8s lost %-4s corrupt %-4s (trunc %-3s) malformed %-3s errb %-3s oe %-3s fe %-3s %s\n", name, sil, ok, lost, cor, tr, mal, eb, oe, fe, $2}' \
    "$SUMMARY" | tee -a "$SUMMARY"
