"""Root helper: check a WiFi network name and password with the Pi's own radio.

The engine runs as an unprivileged user and can't touch the radio, so systemd runs
this as root, one instance per connection on /run/dmx_smartbulb-wifi.sock
(deploy/dmx_smartbulb-wifi.socket). It reads one JSON line {"ssid", "password"}
from stdin and writes one JSON line {"result", "message"} to stdout.

The radio is normally switched off (rfkill). For a check it is switched on, the
network is looked for with a scan, and wpa_supplicant tries the WPA handshake,
which only succeeds with the right password. The Pi doesn't get an address or
join the network, and the radio is switched off again afterwards.

Results: ok, wrong_password, not_found, open (the network has no password),
invalid (the password can't be a WiFi password), unavailable (no radio), error.

Standard library only: it runs with the system python3, not the engine's venv.
"""

import fcntl
import hashlib
import json
import os
import re
import select
import signal
import subprocess
import sys
import time
from pathlib import Path

IFACE = "wlan0"
LOCK = "/run/dmx_smartbulb-wifi.lock"
CONF = "/run/dmx_smartbulb-wifi.conf"
HANDSHAKE_TIMEOUT_S = 20


def classify(line):
    """What a wpa_supplicant output line says about the attempt, or None."""
    if "CTRL-EVENT-CONNECTED" in line:
        return "ok"
    if "WRONG_KEY" in line or "pre-shared key may be incorrect" in line or "4-Way Handshake failed" in line:
        return "wrong_password"
    return None


def parse_scan(text):
    """`iw dev wlan0 scan` output -> {ssid: secured (bool)}."""
    nets, ssid, secured = {}, None, False
    for line in text.splitlines():
        if line.startswith("BSS "):
            if ssid is not None:
                nets[ssid] = nets.get(ssid, False) or secured
            ssid, secured = None, False
        elif line.strip().startswith("SSID:"):
            ssid = line.split("SSID:", 1)[1].strip()
        elif line.strip().startswith(("RSN:", "WPA:")) or "Privacy" in line and "capability" in line:
            secured = True
    if ssid is not None:
        nets[ssid] = nets.get(ssid, False) or secured
    return nets


def wifi_rfkill():
    """sysfs dirs of the WiFi rfkill switches."""
    return [p for p in Path("/sys/class/rfkill").glob("rfkill*") if (p / "type").read_text().strip() == "wlan"]


def run(*cmd, timeout=15):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def scan(ssid):
    for _ in range(5):                  # right after switching on, the radio says "busy" for a moment
        r = run("iw", "dev", IFACE, "scan", "ssid", ssid, timeout=20)
        if r.returncode == 0:
            return parse_scan(r.stdout)
        time.sleep(1)
    raise RuntimeError(f"scan failed: {r.stderr.strip()}")


def handshake(ssid, password):
    psk = hashlib.pbkdf2_hmac("sha1", password.encode(), ssid.encode(), 4096, 32).hex()
    fd = os.open(CONF, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(f"network={{\n ssid={ssid.encode().hex()}\n psk={psk}\n key_mgmt=WPA-PSK\n scan_ssid=1\n}}\n")
    p = subprocess.Popen(["wpa_supplicant", "-i", IFACE, "-D", "nl80211", "-c", CONF],
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    deadline = time.monotonic() + HANDSHAKE_TIMEOUT_S
    buf = ""
    try:
        while (left := deadline - time.monotonic()) > 0:
            if not select.select([p.stdout], [], [], left)[0]:
                break
            chunk = os.read(p.stdout.fileno(), 4096)
            if not chunk:                       # wpa_supplicant exited
                raise RuntimeError(f"wpa_supplicant stopped: {buf.strip()[-200:]}")
            buf += chunk.decode(errors="replace")
            *lines, buf = buf.split("\n")
            for line in lines:
                got = classify(line)
                if got:
                    return got
        return "timeout"
    finally:
        p.terminate()
        try:
            p.wait(3)
        except subprocess.TimeoutExpired:
            p.kill()
        os.unlink(CONF)


def check(ssid, password):
    if not ssid:
        return "invalid", "Enter the network name."
    if password and not (8 <= len(password) <= 63 or re.fullmatch(r"[0-9a-fA-F]{64}", password)):
        return "invalid", "A WiFi password is 8 to 63 characters long."
    if not Path(f"/sys/class/net/{IFACE}").exists():
        return "unavailable", "This Pi's WiFi radio isn't available, so the password can't be checked."
    switches = wifi_rfkill()
    was_blocked = [s for s in switches if (s / "soft").read_text().strip() == "1"]
    try:
        run("rfkill", "unblock", "wifi")
        time.sleep(0.5)
        run("ip", "link", "set", IFACE, "up")
        nets = scan(ssid)
        if ssid not in nets:
            return "not_found", f"No network called \"{ssid}\" was found from here."
        if not nets[ssid]:
            return ("open", f"\"{ssid}\" has no password.") if password else ("ok", f"\"{ssid}\" is an open network.")
        if not password:
            return "wrong_password", f"\"{ssid}\" needs a password."
        got = handshake(ssid, password)
        if got == "ok":
            return "ok", f"The password for \"{ssid}\" is correct."
        if got == "wrong_password":
            return "wrong_password", f"\"{ssid}\" rejected this password."
        return "error", f"\"{ssid}\" didn't answer in time; the password couldn't be checked."
    finally:
        run("ip", "link", "set", IFACE, "down")
        if was_blocked:
            run("rfkill", "block", "wifi")


def main():
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(1))     # still switch the radio off (finally)
    try:
        req = json.loads(sys.stdin.readline() or "{}")
        with open(LOCK, "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)                     # one check at a time
            result, message = check(str(req.get("ssid", "")), str(req.get("password", "")))
    except Exception as e:  # noqa: BLE001 - always answer the engine
        result, message = "error", f"The WiFi check failed: {e}"
    print(f"wifi check: {result}: {message}", file=sys.stderr)
    sys.stdout.write(json.dumps({"result": result, "message": message}) + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
