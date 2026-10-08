#!/bin/bash
# Set up (or check) a Raspberry Pi 4 to run DMX Smart Bulbs.
#   sudo ./setup.sh            install / update; shows the plan and asks before changing anything
#   sudo ./setup.sh --check    report only, change nothing
#   sudo ./setup.sh --help     all options
# Safe to re-run. This bootstrap installs the few apt packages and the Python venv,
# then hands over to `python -m engine setup`, which does the rest.
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"
[ "$(id -u)" -eq 0 ] || { echo "run with sudo" >&2; exit 1; }

check=0
for a in "$@"; do [ "$a" = "--check" ] && check=1; done

if [ $check -eq 0 ]; then
    need=()
    for p in python3-venv avahi-daemon; do
        dpkg -s "$p" >/dev/null 2>&1 || need+=("$p")
    done
    if [ ${#need[@]} -gt 0 ]; then
        echo "installing ${need[*]}"
        apt-get update -q && apt-get install -y -q "${need[@]}"
    fi
    if [ ! -x .venv/bin/python ]; then
        echo "creating .venv"
        python3 -m venv .venv
    fi
    .venv/bin/pip install -q --disable-pip-version-check -r requirements.txt
fi
[ -x .venv/bin/python ] || { echo "no .venv yet: run sudo ./setup.sh first" >&2; exit 1; }
exec .venv/bin/python -m engine setup "$@"
