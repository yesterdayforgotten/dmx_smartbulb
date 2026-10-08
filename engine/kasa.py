"""Kasa (TP-Link) legacy local protocol: JSON obfuscated with an XOR autokey
cipher (initial key 171), as UDP datagrams to port 9999.

Ported from config/kasabulb/kasabulb.py. The payloads are the same; the
transport is now one non-blocking asyncio socket shared by every bulb, so
commands go out together and the bulbs' replies (KL bulbs answer every
command) come back on it for online status and round-trip times.
"""

import asyncio
import json
import socket
import time

KASA_PORT = 9999
LIGHTING = "smartlife.iot.smartbulb.lightingservice"


def encrypt(data):
    if isinstance(data, str):
        data = data.encode()
    key = 171
    out = bytearray(len(data))
    for i, b in enumerate(data):
        key ^= b
        out[i] = key
    return bytes(out)


def decrypt(data):
    key = 171
    out = bytearray(len(data))
    for i, b in enumerate(data):
        out[i] = b ^ key
        key = b
    return out.decode()


def pack(obj):
    """Encode a command compactly (smaller packets matter with 30+ bulbs)."""
    return encrypt(json.dumps(obj, separators=(",", ":")))


def unpack(data):
    return json.loads(decrypt(data))


def scale_hsv(hue, sat, val):
    """DMX bytes (0-255) to Kasa units: hue 0-360, saturation and brightness 0-100.
    Brightness 0-2 rounds to 0, which turns the bulb off. Hue is forced to 180
    when saturation is 0, as the old app did."""
    h = 180 if sat == 0 else (hue * 360) // 255
    return h, (sat * 100) // 255, (val * 100) // 255


def light_state(hue, sat, bri, transition_ms):
    return {LIGHTING: {"transition_light_state": {
        "hue": 180 if sat == 0 else hue,
        "saturation": sat,
        "brightness": bri,
        "on_off": 0 if bri == 0 else 1,
        "color_temp": 0,
        "ignore_default": 1,
        "transition_period": transition_ms,
    }}}


def temperature_state(kelvin, bri, transition_ms):
    return {LIGHTING: {"transition_light_state": {
        "brightness": bri,
        "on_off": 0 if bri == 0 else 1,
        "color_temp": round(kelvin),
        "ignore_default": 1,
        "transition_period": transition_ms,
    }}}


def preferred_state(hue, sat, bri, kelvin=0):
    """Power-on default: hard_on (power applied) uses preset 0, set to this state."""
    return {LIGHTING: {
        "set_default_behavior": {
            "soft_on": {"index": "null", "mode": "last_status"},
            "hard_on": {"index": 0, "mode": "customize_preset"},
        },
        "set_preferred_state": {
            "index": 0,
            "hue": 180 if sat == 0 else hue,
            "saturation": sat,
            "brightness": bri,
            "on_off": 0 if bri == 0 else 1,
            "color_temp": round(kelvin),
        },
    }}


SYSINFO = {"system": {"get_sysinfo": None}}
# Light state only: a ~160-byte reply instead of get_sysinfo's ~1 KB. Used to
# check quiet bulbs are still online without wasting airtime.
LIGHT_STATE = {LIGHTING: {"get_light_state": {}}}
DEFAULT_BEHAVIOR = {LIGHTING: {"get_default_behavior": {}}}
# Several methods ride in one datagram and are all answered in one reply, so
# the power reading costs no extra packets.
IDLE_CHECK = {LIGHTING: {"get_light_state": {}, "get_light_parameters": {}}}
INFO_CHECK = {LIGHTING: {"get_default_behavior": {}, "get_light_parameters": {}}}
LAST_STATE_ON = {LIGHTING: {"set_default_behavior": {"soft_on": {"mode": "last_status"},
                                                     "hard_on": {"mode": "last_status"}}}}
# WiFi (WMM) priority for our packets, as IP TOS bytes. Broadcom APs map the
# top three DSCP bits to an access category: CS5 -> video, CS6 -> voice.
WIFI_PRIORITY_TOS = {"off": 0x00, "video": 0xA0, "voice": 0xC0}


def power_from_reply(reply):
    """(milliwatts, lumens) from a reply that includes get_light_parameters, or None."""
    try:
        p = reply[LIGHTING]["get_light_parameters"]
        return p["energy_usage_milliwatts"], p.get("brightness_lumens")
    except (KeyError, TypeError):
        return None


def power_on_from_reply(reply):
    """Summarise a get_default_behavior reply's power-on (hard_on) setting as
    {"mode": "last"} or {"mode": "preset", "h", "s", "k", "v"}; None if absent."""
    try:
        hard = reply[LIGHTING]["get_default_behavior"]["hard_on"]
    except (KeyError, TypeError):
        return None
    if hard.get("mode") == "last_status":
        return {"mode": "last"}
    return {"mode": "preset", "h": hard.get("hue", 0), "s": hard.get("saturation", 0),
            "k": hard.get("color_temp", 0), "v": hard.get("brightness", 0)}


def sysinfo_mac(info):
    """MAC from a get_sysinfo reply, normalized to 12 upper-case hex digits.
    KL bulbs report it as mic_mac; plugs and switches as mac."""
    raw = info.get("mic_mac") or info.get("mac") or ""
    mac = raw.replace(":", "").replace("-", "").upper()
    return mac if len(mac) == 12 else None


class KasaTransport(asyncio.DatagramProtocol):
    """One UDP socket for all bulbs.

    send() never blocks. Every reply is passed to on_reply(ip, reply, t), and
    request() waits for the next reply from that bulb (used for get_sysinfo).
    """

    def __init__(self, on_reply=None, port=KASA_PORT):
        self.on_reply = on_reply
        self.port = port
        self.transport = None
        self._waiters = {}  # ip -> list of futures
        self.sent = self.send_errors = self.bad_replies = 0

    @classmethod
    async def create(cls, on_reply=None, port=KASA_PORT, bind=("0.0.0.0", 0)):
        loop = asyncio.get_running_loop()
        proto = cls(on_reply, port)
        await loop.create_datagram_endpoint(lambda: proto, local_addr=bind)
        return proto

    def connection_made(self, transport):
        self.transport = transport

    def send(self, ip, command):
        try:
            self.transport.sendto(pack(command), (ip, self.port))
            self.sent += 1
            return True
        except OSError:
            self.send_errors += 1
            return False

    def datagram_received(self, data, addr):
        t = time.monotonic()
        try:
            reply = unpack(data)
        except (ValueError, UnicodeDecodeError):
            self.bad_replies += 1
            return
        ip = addr[0]
        for fut in self._waiters.pop(ip, []):
            if not fut.done():
                fut.set_result(reply)
        if self.on_reply:
            self.on_reply(ip, reply, t)

    def error_received(self, exc):
        # ICMP errors (e.g. port unreachable) for a previous send; not fatal.
        self.send_errors += 1

    async def request(self, ip, command, timeout=0.5):
        """Send and wait for that bulb's next reply; None on timeout."""
        fut = asyncio.get_running_loop().create_future()
        self._waiters.setdefault(ip, []).append(fut)
        self.send(ip, command)
        try:
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            waiters = self._waiters.get(ip, [])
            if fut in waiters:
                waiters.remove(fut)
            return None

    def set_tos(self, tos):
        """Mark our packets with this IP TOS byte (WiFi priority; 0 = normal)."""
        sock = self.transport.get_extra_info("socket") if self.transport else None
        if sock is not None:
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_TOS, tos)

    def close(self):
        if self.transport:
            self.transport.close()


async def discover(targets=("255.255.255.255",), port=KASA_PORT, timeout=2.0, bind=None):
    """Send get_sysinfo to each target (a broadcast address or a list of IPs) and
    collect smart-bulb replies until none arrive for `timeout` seconds.

    Returns {ip: sysinfo dict}. bind=(ip, 0) pins the source address or, with a
    device-bound socket, the interface (used by pairing later).
    """
    loop = asyncio.get_running_loop()
    found = {}
    last = [time.monotonic()]

    class Proto(asyncio.DatagramProtocol):
        def datagram_received(self, data, addr):
            try:
                info = unpack(data)["system"]["get_sysinfo"]
            except (ValueError, KeyError, TypeError, UnicodeDecodeError):
                return
            if info.get("mic_type", info.get("type")) == "IOT.SMARTBULB":
                found[addr[0]] = info
                last[0] = time.monotonic()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.bind(bind or ("0.0.0.0", 0))
    sock.setblocking(False)
    transport, _ = await loop.create_datagram_endpoint(Proto, sock=sock)
    try:
        payload = pack(SYSINFO)
        for ip in targets:
            transport.sendto(payload, (ip, port))
        while time.monotonic() - last[0] < timeout:
            await asyncio.sleep(0.05)
    finally:
        transport.close()
    return found
