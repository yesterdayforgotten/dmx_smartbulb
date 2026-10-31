"""Root service: the cloud relay plus an SNTP responder, so Kasa bulbs keep a cloud
connection (and correct time) with no venue internet. Run by dmx_smartbulb-relay.service.

Listens on the show-network (eth0) address the bulbs reach, forwards the allowed cloud
hostnames out through the WiFi uplink, and answers NTP from the Pi's own clock. Standard
library plus engine.relay only, so it runs under the system python3.
"""
import asyncio, json, logging, socket, struct, subprocess, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from engine.relay import CloudRelay, PUBLIC_RESOLVERS

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
log = logging.getLogger("relay-svc")
STATUS = Path("/dev/shm/dmx_relay.json")
NTP_EPOCH = 2208988800


def wlan_dns():
    try:
        out = subprocess.run(["nmcli", "-g", "IP4.DNS", "device", "show", "wlan0"],
                             capture_output=True, text=True, timeout=5).stdout
        return [x for x in out.replace("|", " ").split() if x and ":" not in x]
    except Exception:
        return []


def iface_ip(name):
    import fcntl
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        return socket.inet_ntoa(fcntl.ioctl(s.fileno(), 0x8915, struct.pack("256s", name.encode()[:15]))[20:24])
    except OSError:
        return None
    finally:
        s.close()


class SNTP(asyncio.DatagramProtocol):
    def __init__(self):
        self.answered = 0
    def connection_made(self, transport):
        self.transport = transport
    def datagram_received(self, data, addr):
        if len(data) < 48:
            return
        now = time.time()
        def stamp(t):
            return struct.pack("!II", int(t) + NTP_EPOCH, int((t % 1) * 2**32))
        ver = (data[0] >> 3) & 7
        reply = bytes([(ver << 3) | 4, 3, data[2], 0xEC]) + b"\0\0\0\0" + b"LOCL" + \
            stamp(now) + data[40:48] + stamp(now) + stamp(time.time())
        self.transport.sendto(reply, addr)
        self.answered += 1


async def main():
    cfg = json.loads(Path(sys.argv[1]).read_text()) if len(sys.argv) > 1 else {}
    relay_cfg = cfg.get("config", cfg).get("relay", {})
    allow = relay_cfg.get("allow") or ["n-devs.tplinkcloud.com"]
    listen_ip = iface_ip("eth0")
    if not listen_ip:
        log.error("no eth0 address; is the show network connected?"); return
    resolvers = wlan_dns() + list(PUBLIC_RESOLVERS)
    log.info("resolvers: %s", resolvers)
    relay = CloudRelay(listen_ip, allow=allow, resolvers=resolvers, out_device="wlan0")
    await relay.start()
    loop = asyncio.get_running_loop()
    sntp = SNTP()
    try:
        await loop.create_datagram_endpoint(lambda: sntp, local_addr=(listen_ip, 123))
        log.info("SNTP responder on %s:123", listen_ip)
    except OSError as e:
        log.warning("SNTP not started: %s", e)
    while True:
        try:
            STATUS.write_text(json.dumps({"t": time.time(), **relay.status(), "ntp_answered": sntp.answered}))
        except OSError:
            pass
        await asyncio.sleep(5)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
