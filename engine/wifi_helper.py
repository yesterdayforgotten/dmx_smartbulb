"""Root helper: everything the engine needs the Pi's WiFi radio for.

The engine runs as an unprivileged user and can't touch the radio, so systemd runs
this as root, one instance per connection on /run/dmx_smartbulb-wifi.sock
(deploy/dmx_smartbulb-wifi.socket). It reads one JSON request line from stdin and
writes one JSON line {"result", "message", ...} to stdout.

The radio is normally switched off (rfkill); each request switches it on and back off.

  {"ssid", "password"}           check a network name and password: scan for it and
                                 try the WPA handshake, which only succeeds with the
                                 right password. The Pi doesn't get an address.
                                 ok, wrong_password, not_found, open, invalid,
                                 unavailable, error
  {"op": "scan_setup"}           list bulbs in setup mode (their open setup networks).
                                 ok + "networks", unavailable, error
  {"op": "pair", "setup_ssid",   join a bulb's setup network and send it the show
   "ssid", "password", "alias"}  network: sent + mac, model, fw; not_found, invalid,
                                 unavailable, error. The engine then looks for the
                                 bulb on the show network to confirm it joined.

The engine's connection to the bulbs is on Ethernet and isn't touched: the setup
network's route has a high metric and our socket is bound to wlan0, so this works
even if the show network is also 192.168.0.x.
"""

import contextlib
import fcntl
import hashlib
import json
import os
import re
import select
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from engine import kasa  # noqa: E402

IFACE = "wlan0"
LOCK = "/run/dmx_smartbulb-wifi.lock"
CONF = "/run/dmx_smartbulb-wifi.conf"
HANDSHAKE_TIMEOUT_S = 20
SETUP_SSID = re.compile(r"^(TP-LINK_Smart Bulb_|Kasa Smart Bulb)", re.I)
BULB_IP = "192.168.0.1"       # a bulb's own address on its setup network
PAIR_IP = "192.168.0.200"     # ours there; bulbs would give us 192.168.0.x by DHCP too


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
        elif line.strip().startswith(("RSN:", "WPA:")) or "Capability:" in line and "Privacy" in line:
            secured = True
    if ssid is not None:
        nets[ssid] = nets.get(ssid, False) or secured
    return nets


def wifi_rfkill():
    """sysfs dirs of the WiFi rfkill switches."""
    return [p for p in Path("/sys/class/rfkill").glob("rfkill*") if (p / "type").read_text().strip() == "wlan"]


def run(*cmd, timeout=15):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def scan(ssid=None):
    cmd = ["iw", "dev", IFACE, "scan"] + (["ssid", ssid] if ssid else [])
    for _ in range(5):                  # right after switching on, the radio says "busy" for a moment
        r = run(*cmd, timeout=20)
        if r.returncode == 0:
            return parse_scan(r.stdout)
        time.sleep(1)
    raise RuntimeError(f"scan failed: {r.stderr.strip()}")


def have_radio():
    return Path(f"/sys/class/net/{IFACE}").exists()


NO_RADIO = "This Pi's WiFi radio isn't available."


@contextlib.contextmanager
def radio():
    """The radio on for the duration, then off again with nothing left on wlan0."""
    was_blocked = [s for s in wifi_rfkill() if (s / "soft").read_text().strip() == "1"]
    try:
        run("rfkill", "unblock", "wifi")
        time.sleep(0.5)
        run("ip", "link", "set", IFACE, "up")
        yield
    finally:
        run("ip", "addr", "flush", "dev", IFACE)
        run("ip", "link", "set", IFACE, "down")
        if was_blocked:
            run("rfkill", "block", "wifi")


@contextlib.contextmanager
def wpa_supplicant(network):
    """wpa_supplicant running on the radio with one network block (a list of lines)."""
    fd = os.open(CONF, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write("network={\n" + "".join(f" {ln}\n" for ln in network) + "}\n")
    p = subprocess.Popen(["wpa_supplicant", "-i", IFACE, "-D", "nl80211", "-c", CONF],
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        yield p
    finally:
        p.terminate()
        try:
            p.wait(3)
        except subprocess.TimeoutExpired:
            p.kill()
        os.unlink(CONF)


def wait_for_outcome(p):
    """Read wpa_supplicant's output until classify() has an answer or time runs out."""
    deadline = time.monotonic() + HANDSHAKE_TIMEOUT_S
    buf = ""
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


def handshake(ssid, password):
    psk = hashlib.pbkdf2_hmac("sha1", password.encode(), ssid.encode(), 4096, 32).hex()
    with wpa_supplicant([f"ssid={ssid.encode().hex()}", f"psk={psk}", "key_mgmt=WPA-PSK", "scan_ssid=1"]) as p:
        return wait_for_outcome(p)


def password_problem(password):
    if password and not (8 <= len(password) <= 63 or re.fullmatch(r"[0-9a-fA-F]{64}", password)):
        return "A WiFi password is 8 to 63 characters long."
    return None


def check(ssid, password):
    if not ssid:
        return "invalid", "Enter the network name."
    if problem := password_problem(password):
        return "invalid", problem
    if not have_radio():
        return "unavailable", "This Pi's WiFi radio isn't available, so the password can't be checked."
    with radio():
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


def scan_setup():
    if not have_radio():
        return {"result": "unavailable", "message": NO_RADIO, "networks": []}
    with radio():
        nets = scan()
    found = sorted(n for n, secured in nets.items() if SETUP_SSID.match(n) and not secured)
    return {"result": "ok", "message": f"{len(found)} bulb(s) in setup mode.", "networks": found}


def talk(sock, addr, command, tries=3, timeout=1.5):
    """Send a command and return the decoded reply, or None if none came."""
    sock.settimeout(timeout)
    for _ in range(tries):
        sock.sendto(kasa.pack(command), addr)
        try:
            return kasa.unpack(sock.recvfrom(4096)[0])
        except socket.timeout:
            continue
        except ValueError:
            continue
    return None


def onboard(sock, addr, ssid, password, alias=""):
    """The conversation with a bulb on its setup network. Raises RuntimeError with a
    message for the user if it isn't a bulb we can set up."""
    reply = talk(sock, addr, kasa.SYSINFO, tries=5)
    if reply is None:
        raise RuntimeError("the bulb didn't answer on its setup network")
    info = reply.get("system", {}).get("get_sysinfo", {})
    if info.get("mic_type", info.get("type")) != "IOT.SMARTBULB":
        raise RuntimeError(f"that device isn't a supported smart bulb ({info.get('model', 'unknown model')})")
    if alias:
        talk(sock, addr, kasa.set_alias(alias))
    # The bulb may leave its setup network before replying, so no reply is normal.
    reply = talk(sock, addr, kasa.join_wifi(ssid, password), tries=2, timeout=2.0)
    if reply:
        answer = reply.get("smartlife.iot.common.softaponboarding", {}).get("set_stainfo", {})
        if answer.get("err_code", 0) != 0:
            raise RuntimeError(f"the bulb refused the network settings: {answer.get('err_msg', answer['err_code'])}")
    return {"mac": kasa.sysinfo_mac(info), "model": info.get("model"), "fw": info.get("sw_ver")}


def pair(setup_ssid, ssid, password, alias=""):
    if not SETUP_SSID.match(setup_ssid or ""):
        return {"result": "invalid", "message": f"\"{setup_ssid}\" isn't a bulb's setup network."}
    if not ssid:
        return {"result": "invalid", "message": "Enter the show network's name first."}
    if problem := password_problem(password):
        return {"result": "invalid", "message": problem}
    if not have_radio():
        return {"result": "unavailable", "message": NO_RADIO}
    with radio():
        if setup_ssid not in scan(setup_ssid):
            return {"result": "not_found", "message": f"\"{setup_ssid}\" wasn't found. Is the bulb still in setup mode?"}
        with wpa_supplicant([f"ssid={setup_ssid.encode().hex()}", "key_mgmt=NONE", "scan_ssid=1"]) as p:
            if wait_for_outcome(p) != "ok":
                return {"result": "error", "message": f"Couldn't join \"{setup_ssid}\"."}
            Path(f"/proc/sys/net/ipv4/conf/{IFACE}/rp_filter").write_text("0")
            run("ip", "addr", "add", f"{PAIR_IP}/24", "dev", IFACE, "noprefixroute")
            run("ip", "route", "add", "192.168.0.0/24", "dev", IFACE, "src", PAIR_IP, "metric", "5000")
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, IFACE.encode())
                sock.bind((PAIR_IP, 0))
                bulb = onboard(sock, (BULB_IP, kasa.KASA_PORT), ssid, password, alias)
    return {"result": "sent", "message": f"Sent the network settings to {bulb['model']}.", **bulb}


def handle(req):
    op = req.get("op", "check")
    if op == "check":
        result, message = check(str(req.get("ssid", "")), str(req.get("password", "")))
        return {"result": result, "message": message}
    if op == "scan_setup":
        return scan_setup()
    if op == "pair":
        return pair(str(req.get("setup_ssid", "")), str(req.get("ssid", "")), str(req.get("password", "")),
                    str(req.get("alias", ""))[:kasa.ALIAS_MAX])
    return {"result": "invalid", "message": f"unknown request {op!r}"}


def main():
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(1))     # still switch the radio off (finally)
    try:
        req = json.loads(sys.stdin.readline() or "{}")
        with open(LOCK, "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)                     # one at a time: there's one radio
            reply = handle(req)
    except Exception as e:  # noqa: BLE001 - always answer the engine
        reply = {"result": "error", "message": f"The WiFi request failed: {e}"}
    print(f"wifi {req.get('op', 'check') if 'req' in locals() else '?'}: {reply['result']}: {reply['message']}", file=sys.stderr)
    sys.stdout.write(json.dumps(reply) + "\n")
    sys.stdout.flush()


if __name__ == "__main__":
    main()
