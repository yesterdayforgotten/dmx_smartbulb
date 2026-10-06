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

# Run esptool <cmd> (write_flash or verify_flash) over the image FILE, skipping
# the sectors listed in FILE.bad: the image is cut into one piece per good range.
flash_ranges() {
    local file=$1 cmd=$2 pieces dir start=0 size end args=() extra=()
    size=$(stat -c %s "$file")
    dir=$(mktemp -d)
    pieces=$( { [ -f "$file.bad" ] && cat "$file.bad"; echo "$size"; } | while read -r b; do
        b=$((b)); [ "$b" -gt "$start" ] && echo "$start $((b - start))"; start=$((b + 0x1000)); done )
    while read -r off len; do
        dd if="$file" of="$dir/$off.bin" bs=4096 skip=$((off / 4096)) count=$((len / 4096)) status=none
        args+=("$off" "$dir/$off.bin")
    done <<<"$pieces"
    [ "$cmd" = write_flash ] && extra=(--flash_mode keep --flash_freq keep --flash_size keep)
    timeout 600 "$PIO_PY" "$ESPTOOL" --chip esp32 --port "$PORT" --baud 460800 "$cmd" "${extra[@]}" "${args[@]}" \
        | grep -vE "\([0-9]+ %\)" | tail -4
    local rc=${PIPESTATUS[0]}
    rm -rf "$dir"
    return "$rc"
}

newest_backup() { ls -1t "$BACKUPS"/esp32-"$MAC"-*.bin 2>/dev/null | head -1; }

case ${1:-} in
backup)
    board_info
    mkdir -p "$BACKUPS"
    f=$BACKUPS/esp32-$MAC-$(date +%Y%m%d-%H%M%S).bin
    echo "board $MAC, flash $SIZE: reading (esptool checks the chip's MD5 of the read)"
    # esptool's read has no timeout and hangs on an unreadable flash sector, so
    # read in chunks with a timeout. A chunk that fails is re-read sector by
    # sector; sectors that still fail are filled with 0xFF and listed in $f.bad
    # (restore skips them).
    CHUNK=$((0x40000)) SECTOR=$((0x1000))
    tmp=$(mktemp -d)
    : > "$tmp/bad"
    readpart() {  # offset size file baud
        timeout "$5" "$PIO_PY" "$ESPTOOL" --chip esp32 --port "$PORT" --baud "$4" \
            read_flash "$1" "$2" "$3" >/dev/null 2>&1
    }
    for ((off = 0; off < BYTES; off += CHUNK)); do
        part=$(printf '%s/%08x.bin' "$tmp" "$off")
        if readpart "$off" "$CHUNK" "$part" "$BAUD" 20 || readpart "$off" "$CHUNK" "$part" 460800 30; then
            printf '  read 0x%06x-0x%06x\n' "$off" $((off + CHUNK))
            continue
        fi
        printf '  chunk 0x%06x failed; reading it sector by sector\n' "$off"
        : > "$part"
        for ((s = off; s < off + CHUNK; s += SECTOR)); do
            if readpart "$s" "$SECTOR" "$tmp/sector" 460800 8 || readpart "$s" "$SECTOR" "$tmp/sector" 115200 15; then
                cat "$tmp/sector" >> "$part"
            else
                printf '0x%06x\n' "$s" | tee -a "$tmp/bad" | sed 's/^/  UNREADABLE sector /'
                head -c "$SECTOR" /dev/zero | tr '\0' '\377' >> "$part"
            fi
        done
    done
    cat "$tmp"/*.bin > "$f"
    [ "$(stat -c %s "$f")" -eq $((BYTES)) ] || { echo "image has the wrong size" >&2; exit 1; }
    if [ -s "$tmp/bad" ]; then
        cp "$tmp/bad" "$f.bad"
        echo "$(wc -l < "$f.bad") unreadable sector(s), listed in $f.bad (filled with 0xFF in the image)"
    fi
    rm -rf "$tmp"
    echo "verifying the image against the chip"
    flash_ranges "$f" verify_flash
    (cd "$BACKUPS" && sha256sum "$(basename "$f")" > "$(basename "$f").sha256")
    echo "backup OK: $f"
    cat "$f.sha256"
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
    [ -f "$f.bad" ] && echo "skipping unreadable sectors: $(tr '\n' ' ' < "$f.bad")"
    flash_ranges "$f" write_flash
    flash_ranges "$f" verify_flash
    ;;
*)
    sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'
    exit 1
    ;;
esac
