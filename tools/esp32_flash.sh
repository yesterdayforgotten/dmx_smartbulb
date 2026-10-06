#!/bin/bash
# Back up, flash and restore the ESP32 over USB, using PlatformIO's bundled esptool.
#
#   tools/esp32_flash.sh backup            dump the whole flash to ~/esp32_backups/
#   tools/esp32_flash.sh txtest            flash dmx_esp32_txtest (refuses without a backup of this board)
#   tools/esp32_flash.sh restore [FILE]    write a backup back (default: newest for this board)
#
# PORT=/dev/ttyUSB0 and BAUD=921600 can be overridden from the environment.
set -euo pipefail

PORT=${PORT:-/dev/ttyUSB0}
BAUD=${BAUD:-921600}
BACKUPS=${BACKUPS:-$HOME/esp32_backups}
REPO=$(cd "$(dirname "$0")/.." && pwd)
PIO_PY=$HOME/.platformio/penv/bin/python
ESPTOOL=$HOME/.platformio/packages/tool-esptoolpy/esptool.py

esptool() { "$PIO_PY" "$ESPTOOL" --chip esp32 --port "$PORT" --baud "$BAUD" "$@"; }

[ -e "$PORT" ] || { echo "no $PORT: plug the ESP32 into the Pi by USB" >&2; exit 1; }

board_info() {
    local out
    out=$(esptool flash_id 2>&1) || { echo "$out" >&2; exit 1; }
    MAC=$(sed -n 's/^MAC: //p' <<<"$out" | head -1 | tr -d ':')
    SIZE=$(sed -n 's/^Detected flash size: //p' <<<"$out" | head -1)
    case $SIZE in
        2MB) BYTES=0x200000 ;; 4MB) BYTES=0x400000 ;; 8MB) BYTES=0x800000 ;; 16MB) BYTES=0x1000000 ;;
        *) echo "unexpected flash size '$SIZE'" >&2; exit 1 ;;
    esac
    [ -n "$MAC" ] || { echo "couldn't read the board's MAC" >&2; exit 1; }
}

newest_backup() { ls -1t "$BACKUPS"/esp32-"$MAC"-*.bin 2>/dev/null | head -1; }

case ${1:-} in
backup)
    board_info
    mkdir -p "$BACKUPS"
    f=$BACKUPS/esp32-$MAC-$(date +%Y%m%d-%H%M%S).bin
    echo "board $MAC, flash $SIZE: reading twice to verify"
    esptool read_flash 0 "$BYTES" "$f"
    esptool read_flash 0 "$BYTES" "$f.check"
    if cmp -s "$f" "$f.check"; then
        rm "$f.check"
        (cd "$BACKUPS" && sha256sum "$(basename "$f")" > "$(basename "$f").sha256")
        echo "backup OK: $f"
        cat "$f.sha256"
    else
        echo "the two reads differ; backup NOT trusted ($f, $f.check)" >&2
        exit 1
    fi
    ;;
txtest)
    board_info
    b=$(newest_backup)
    [ -n "$b" ] || { echo "no backup for board $MAC in $BACKUPS; run '$0 backup' first" >&2; exit 1; }
    (cd "$BACKUPS" && sha256sum -c --quiet "$(basename "$b").sha256") || { echo "backup $b fails its checksum" >&2; exit 1; }
    echo "backup present: $b"
    cd "$REPO/dmx_esp32_txtest"
    "$HOME/.platformio/penv/bin/pio" run -t upload --upload-port "$PORT"
    ;;
restore)
    board_info
    f=${2:-$(newest_backup)}
    [ -n "$f" ] && [ -f "$f" ] || { echo "no backup to restore for board $MAC" >&2; exit 1; }
    if [ -f "$f.sha256" ]; then
        (cd "$(dirname "$f")" && sha256sum -c --quiet "$(basename "$f").sha256")
    fi
    echo "restoring $f"
    esptool write_flash --flash_mode keep --flash_freq keep --flash_size keep 0 "$f"
    esptool verify_flash 0 "$f"
    ;;
*)
    sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'
    exit 1
    ;;
esac
