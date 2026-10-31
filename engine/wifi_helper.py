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
  {"op": "venue_status"}         is wlan0 on a network? connected, ssid, ip, internet,
                                 temporary, saved (WiFi profiles NetworkManager keeps)
  {"op": "venue_networks"}       names of nearby networks
  {"op": "venue_join", "ssid",   put the Pi on the venue's WiFi for internet, Ethernet
   "password"}                   to the bulbs untouched. Everything is kept in /run, so
                                 a reboot undoes it: see venue_join().
  {"op": "venue_leave"}          drop it and switch the radio off
  {"op": "boot_reset"}           at engine start: radio off unless WiFi is in use
  {"op": "pair", "setup_ssid",   join a bulb's setup network and send it the show
   "ssid", "password", "alias"}  network: sent + mac, model, fw; not_found, invalid,
                                 unavailable, error. The engine then looks for the
                                 bulb on the show network to confirm it joined.

The other radio uses take wlan0 over, so they refuse while it has an address.

The engine's connection to the bulbs is on Ethernet and isn't touched: the setup
network's route has a high metric and our socket is bound to wlan0, so this works
even if the show network is also 192.168.0.x.
"""

import contextlib
import fcntl
import hashlib
import ipaddress
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


VENUE_ID = "dmx_smartbulb-venue"
VENUE_CONF = Path("/run/NetworkManager/conf.d/95-dmx_smartbulb-venue.conf")
VENUE_PROFILE = Path(f"/run/NetworkManager/system-connections/{VENUE_ID}.nmconnection")
IN_USE = "The Pi is connected to WiFi. Turn that off first (Setup → Venue WiFi)."


def wifi_in_use():
    """wlan0 has an address, so taking it over would cut that connection."""
    return bool(run("ip", "-4", "-o", "addr", "show", "dev", IFACE).stdout.strip())


def venue_keyfile(ssid, password):
    """A NetworkManager profile that never starts by itself. The SSID is written as bytes,
    so any name is safe in the file."""
    lines = ["[connection]", f"id={VENUE_ID}", "type=wifi", f"interface-name={IFACE}", "autoconnect=false", "",
             "[wifi]", "mode=infrastructure", "ssid=" + "".join(f"{b};" for b in ssid.encode()), ""]
    if password:
        escaped = password.replace("\\", "\\\\")
        lines += ["[wifi-security]", "key-mgmt=wpa-psk", f"psk={escaped}", ""]
    # A lower metric than Ethernet's 100: the default route is the one with internet.
    lines += ["[ipv4]", "method=auto", "route-metric=50", "", "[ipv6]", "method=disabled", ""]
    return "\n".join(lines)


def subnet_clash(address, others):
    """Does this "ip/prefix" overlap any other interface's? If so the bulbs' traffic
    could go out over WiFi."""
    net = ipaddress.ip_interface(address).network
    return any(net.overlaps(ipaddress.ip_interface(o).network) for o in others)


def addresses():
    """{interface: "ip/prefix"} for every IPv4 address except loopback."""
    out = {}
    for line in run("ip", "-4", "-o", "addr", "show").stdout.splitlines():
        f = line.split()
        if f[1] != "lo":
            out[f[1]] = f[3]
    return out


def wifi_profiles():
    rows = run("nmcli", "-t", "-f", "NAME,TYPE", "connection", "show").stdout.splitlines()
    return [r.rsplit(":", 1)[0] for r in rows if r.endswith(":802-11-wireless")]


def venue_status():
    addrs = addresses()
    connected = IFACE in addrs
    profile = run("nmcli", "-g", "GENERAL.CONNECTION", "device", "show", IFACE).stdout.strip() if connected else ""
    # Our temporary profile has a fixed id, so ask it for the network's real name.
    ssid = profile
    if profile == VENUE_ID:
        ssid = run("nmcli", "-g", "802-11-wireless.ssid", "connection", "show", "id", VENUE_ID).stdout.strip() or profile
    saved = [n for n in wifi_profiles() if n != VENUE_ID]
    message = f"Connected to {ssid}." if connected else "Not connected."
    reply = {"result": "ok", "message": message, "connected": connected, "ssid": ssid, "ip": addrs.get(IFACE, ""),
             "temporary": profile == VENUE_ID, "saved": saved, "internet": ""}
    if connected:
        reply["internet"] = run("nmcli", "networking", "connectivity", "check", timeout=20).stdout.strip()
    return reply


def venue_networks():
    if not have_radio():
        return {"result": "unavailable", "message": NO_RADIO, "networks": []}
    if wifi_in_use():                   # read the kernel's list: a scan could disturb the connection
        nets = parse_scan(run("iw", "dev", IFACE, "scan", "dump").stdout)
    else:
        with radio():
            nets = scan()
    names = sorted((n for n in nets if n and not SETUP_SSID.match(n)), key=str.lower)
    return {"result": "ok", "message": "", "networks": names}


def venue_cleanup():
    run("nmcli", "connection", "delete", "id", VENUE_ID)
    VENUE_PROFILE.unlink(missing_ok=True)
    VENUE_CONF.unlink(missing_ok=True)
    run("systemctl", "reload", "NetworkManager")
    time.sleep(1)
    run("nmcli", "device", "set", IFACE, "managed", "no")
    run("ip", "addr", "flush", "dev", IFACE)
    run("ip", "link", "set", IFACE, "down")
    run("rfkill", "block", "wifi")


def venue_join(ssid, password):
    """Setup keeps NetworkManager off wlan0 with a file in /usr/lib that `nmcli device set`
    can't override. /run is read after it, so a file there lifts it (one in /etc would win
    over /run), and the profile lives in /run too.
    A reboot clears both, and boot_reset() switches the radio back off."""
    if not ssid:
        return {"result": "invalid", "message": "Enter the network name."}
    if re.search(r"[\x00-\x1f]", ssid + password):
        return {"result": "invalid", "message": "The name or password has a character that can't be used."}
    if problem := password_problem(password):
        return {"result": "invalid", "message": problem}
    if not have_radio():
        return {"result": "unavailable", "message": NO_RADIO}
    if wifi_in_use():
        return {"result": "invalid", "message": IN_USE}
    VENUE_CONF.parent.mkdir(parents=True, exist_ok=True)
    VENUE_CONF.write_text("# Written by engine/wifi_helper.py; gone at the next reboot.\n[keyfile]\nunmanaged-devices=\n")
    fd = os.open(VENUE_PROFILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(venue_keyfile(ssid, password))
    try:
        run("systemctl", "reload", "NetworkManager")
        run("rfkill", "unblock", "wifi")
        time.sleep(1)
        run("nmcli", "device", "set", IFACE, "managed", "yes")
        time.sleep(2)
        run("nmcli", "device", "wifi", "list", "--rescan", "yes", timeout=20)
        loaded = run("nmcli", "connection", "load", str(VENUE_PROFILE))
        if loaded.returncode:
            raise RuntimeError(loaded.stderr.strip() or "NetworkManager wouldn't load the profile")
        up = run("nmcli", "--wait", "40", "connection", "up", "id", VENUE_ID, timeout=50)
        if up.returncode:
            raise RuntimeError(up.stderr.strip() or "it didn't connect")
        addrs = addresses()
        if IFACE not in addrs:
            raise RuntimeError("it connected but got no address")
        if subnet_clash(addrs[IFACE], [a for i, a in addrs.items() if i != IFACE]):
            raise RuntimeError(f"its addresses ({addrs[IFACE]}) overlap the bulbs' network, which would cut the bulbs off")
    except (RuntimeError, subprocess.TimeoutExpired) as e:
        venue_cleanup()
        return {"result": "error", "message": f"Couldn't join {ssid}: {e}. WiFi is off again."}
    return {**venue_status(), "message": f"Connected to {ssid}."}


def venue_leave():
    was = run("nmcli", "-g", "GENERAL.CONNECTION", "device", "show", IFACE).stdout.strip()
    run("nmcli", "device", "disconnect", IFACE)
    venue_cleanup()
    saved = [n for n in wifi_profiles() if n != VENUE_ID]
    note = f" Saved network{'s' if len(saved) > 1 else ''} {', '.join(saved)} can still connect after a reboot." if saved else ""
    return {"result": "ok", "message": f"WiFi off{f' (was {was})' if was and was != VENUE_ID else ''}.{note}",
            "connected": False, "ssid": "", "ip": "", "temporary": False, "saved": saved, "internet": ""}


def boot_reset():
    """Radio off, unless WiFi is in use or a saved profile may be about to connect."""
    if have_radio() and not wifi_in_use() and not wifi_profiles():
        run("rfkill", "block", "wifi")
    return {"result": "ok", "message": ""}


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
    if wifi_in_use():
        return "unavailable", "The password wasn't checked because the Pi is connected to WiFi."
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
    if wifi_in_use():
        return {"result": "unavailable", "message": IN_USE, "networks": []}
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
    if wifi_in_use():
        return {"result": "unavailable", "message": IN_USE}
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


UPLINK_ID = "dmx_smartbulb-uplink"
UPLINK_PROFILE = Path(f"/etc/NetworkManager/system-connections/{UPLINK_ID}.nmconnection")
REPO = Path(__file__).resolve().parent.parent


def uplink_keyfile(ssid, password):
    """A persistent NetworkManager profile for the relay's internet uplink: autoconnects
    at boot, low route metric so the wired show network keeps the default LAN."""
    lines = ["[connection]", f"id={UPLINK_ID}", "type=wifi", f"interface-name={IFACE}",
             "autoconnect=true", "autoconnect-priority=10", "",
             "[wifi]", "mode=infrastructure", "ssid=" + "".join(f"{b};" for b in ssid.encode()), ""]
    if password:
        esc = password.replace("\\", "\\\\")
        lines += ["[wifi-security]", "key-mgmt=wpa-psk", f"psk={esc}", ""]
    lines += ["[ipv4]", "method=auto", "route-metric=50", "", "[ipv6]", "method=disabled", ""]
    return "\n".join(lines)


def apply_firewall():
    script = REPO / "deploy" / "wlan0-firewall.sh"
    if script.exists():
        run("sh", str(script))


def uplink_status():
    addrs = addresses()
    connected = IFACE in addrs
    conn = run("nmcli", "-g", "GENERAL.CONNECTION", "device", "show", IFACE).stdout.strip() if connected else ""
    m = re.search(r"SSID: (.+)", run("iw", "dev", IFACE, "link").stdout)
    ssid = (m.group(1).strip() if m else conn) if connected else ""
    internet = run("nmcli", "networking", "connectivity", "check", timeout=15).stdout.strip() if connected else ""
    return {"result": "ok", "message": "", "connected": connected, "ssid": ssid, "ip": addrs.get(IFACE, ""),
            "internet": internet, "managed": conn == UPLINK_ID}


def uplink_set(ssid, password):
    if not ssid:
        return {"result": "invalid", "message": "Enter the network name."}
    if re.search(r"[\x00-\x1f]", ssid + password):
        return {"result": "invalid", "message": "The name or password has a character that can't be used."}
    if problem := password_problem(password):
        return {"result": "invalid", "message": problem}
    if not have_radio():
        return {"result": "unavailable", "message": NO_RADIO}
    run("rfkill", "unblock", "wifi")
    run("nmcli", "device", "set", IFACE, "managed", "yes")
    fd = os.open(UPLINK_PROFILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(uplink_keyfile(ssid, password))
    for name in wifi_profiles():                      # only our profile autoconnects at boot
        if name != UPLINK_ID:
            run("nmcli", "connection", "modify", name, "connection.autoconnect", "no")
    run("nmcli", "connection", "reload")
    up = run("nmcli", "--wait", "40", "connection", "up", UPLINK_ID, timeout=50)
    if up.returncode:
        return {"result": "error", "message": up.stderr.strip() or f"Couldn't join {ssid}."}
    addrs = addresses()
    if IFACE not in addrs:
        return {"result": "error", "message": "Connected but got no address from the network."}
    if subnet_clash(addrs[IFACE], [a for i, a in addrs.items() if i != IFACE]):
        return {"result": "error", "message": f"Its address ({addrs[IFACE]}) overlaps the bulbs' network."}
    apply_firewall()
    return {**uplink_status(), "message": f"Connected to {ssid}."}


def uplink_forget():
    run("nmcli", "connection", "delete", "id", UPLINK_ID)
    UPLINK_PROFILE.unlink(missing_ok=True)
    return {"result": "ok", "message": "Uplink removed.", "connected": False, "ssid": "", "ip": "", "internet": "", "managed": False}


def uplink_networks():
    if not have_radio():
        return {"result": "unavailable", "message": NO_RADIO, "networks": []}
    out = run("nmcli", "-t", "-f", "SSID,SIGNAL", "device", "wifi", "list", "--rescan", "auto", timeout=20).stdout
    seen = {}
    for line in out.splitlines():
        name = line.rsplit(":", 1)[0]
        try:
            sig = int(line.rsplit(":", 1)[1])
        except (ValueError, IndexError):
            sig = 0
        if name and (name not in seen or sig > seen[name]):
            seen[name] = sig
    return {"result": "ok", "message": "", "networks": [n for n, _ in sorted(seen.items(), key=lambda kv: -kv[1])]}


def handle(req):
    op = req.get("op", "check")
    if op == "check":
        result, message = check(str(req.get("ssid", "")), str(req.get("password", "")))
        return {"result": result, "message": message}
    if op == "scan_setup":
        return scan_setup()
    if op == "venue_status":
        return venue_status()
    if op == "venue_networks":
        return venue_networks()
    if op == "venue_join":
        return venue_join(str(req.get("ssid", "")), str(req.get("password", "")))
    if op == "venue_leave":
        return venue_leave()
    if op == "boot_reset":
        return boot_reset()
    if op == "uplink_status":
        return uplink_status()
    if op == "uplink_networks":
        return uplink_networks()
    if op == "uplink_set":
        return uplink_set(str(req.get("ssid", "")), str(req.get("password", "")))
    if op == "uplink_forget":
        return uplink_forget()
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
