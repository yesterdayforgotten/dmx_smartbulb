"""Cloud relay: give Kasa bulbs a working cloud connection with no venue internet.

Firmware 1.0.15 on the KL135 drops off WiFi for ~1 minute every ~10 minutes whenever it
can't reach TP-Link's cloud (a known TP-Link behavior). The bulbs keep their light on
during the drop but ignore DMX, which is fatal for a show. The only thing that stops it
is a real cloud connection.

This is a transparent TCP proxy. The venue router's DNS points the bulbs' cloud names at
the Pi; the bulbs open a TLS connection to the Pi thinking it's the cloud; the Pi forwards
the bytes, unread, to the real TP-Link server over its own WiFi uplink. The bulb checks
the server's certificate, which is still TP-Link's (we never touch the TLS), so the bulb
is satisfied and keeps its heartbeat alive. See deploy/ and the README for the router side.

Only the entry/login server traps unbound bulbs in a retry loop, so by default we forward
only `n-devs...` (the main server) and let `n-deventry...` fail, which makes the bulb fall
straight through to the main server. The allowed names are configurable.

The relay resolves the real server itself, through a fixed resolver over the WiFi uplink,
never the router (whose answer would be the Pi, a loop). Any answer inside a local subnet,
or the Pi itself, is rejected for the same reason.
"""

import asyncio
import ipaddress
import logging
import socket
import struct
import time

log = logging.getLogger("relay")

CLOUD_PORTS = (443, 50443, 60443)
DEFAULT_ALLOW = ("n-devs.tplinkcloud.com",)       # not n-deventry: it traps unbound bulbs
PUBLIC_RESOLVERS = ("1.1.1.1", "8.8.8.8")
DNS_TIMEOUT = 4.0
CONNECT_TIMEOUT = 10.0


def parse_sni(data):
    """The server name from a TLS ClientHello, or None if this isn't one."""
    try:
        if data[0] != 0x16 or data[5] != 0x01:           # handshake record, ClientHello
            return None
        i = 43                                           # past fixed header + random
        i += 1 + data[i]                                 # session id
        i += 2 + struct.unpack_from(">H", data, i)[0]    # cipher suites
        i += 1 + data[i]                                 # compression methods
        end = i + 2 + struct.unpack_from(">H", data, i)[0]
        i += 2
        while i + 4 <= end:
            etype, elen = struct.unpack_from(">H", data, i), struct.unpack_from(">H", data, i + 2)[0]
            if etype[0] == 0:                            # server_name
                return data[i + 9:i + 4 + elen].decode("idna")
            i += 4 + elen
    except (IndexError, struct.error, UnicodeError):
        pass
    return None


def is_local(ip):
    """An address the relay must never dial: private, loopback, link-local, unspecified.
    The real cloud is public, so a local answer means the router's loop."""
    a = ipaddress.ip_address(ip)
    return a.is_private or a.is_loopback or a.is_link_local or a.is_unspecified or a.is_multicast


def _dns_query(host, resolver, device=None):
    """One A-record lookup straight to `resolver` over UDP (optionally pinned to a device),
    bypassing the system resolver and the router. Returns a list of IPv4 strings."""
    qname = b"".join(bytes([len(p)]) + p.encode() for p in host.split(".")) + b"\x00"
    pkt = struct.pack(">HHHHHH", 0x1234, 0x0100, 1, 0, 0, 0) + qname + struct.pack(">HH", 1, 1)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        if device:
            try:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, device.encode())
            except (OSError, PermissionError):
                pass
        s.settimeout(DNS_TIMEOUT)
        s.sendto(pkt, (resolver, 53))
        data, _ = s.recvfrom(2048)
    finally:
        s.close()
    n = struct.unpack_from(">H", data, 6)[0]              # answer count
    i = 12 + len(qname) + 4
    out = []
    for _ in range(n):
        if data[i] & 0xC0 == 0xC0:
            i += 2
        else:
            while data[i]:
                i += 1 + data[i]
            i += 1
        rtype, _, _, rdlen = struct.unpack_from(">HHIH", data, i)
        i += 10
        if rtype == 1 and rdlen == 4:
            out.append(socket.inet_ntoa(data[i:i + 4]))
        i += rdlen
    return out


class CloudRelay:
    def __init__(self, listen_ip, allow=DEFAULT_ALLOW, resolvers=PUBLIC_RESOLVERS,
                 ports=CLOUD_PORTS, out_device=None):
        self.listen_ip = listen_ip
        self.allow = set(allow)
        self.resolvers = list(resolvers)
        self.ports = tuple(ports)
        self.out_device = out_device
        self.servers = []
        self.started = 0.0
        self.stats = {"forwarded": 0, "refused": 0, "resolve_fail": 0, "dial_fail": 0,
                      "bytes_up": 0, "bytes_down": 0, "open": 0}
        self.bulbs = set()                               # bulb IPs seen forwarding
        self._resolved = {}                              # host -> (ip, monotonic)

    async def resolve(self, host):
        """A real public IP for `host`, cached briefly, never a local/loop answer."""
        hit = self._resolved.get(host)
        if hit and time.monotonic() - hit[1] < 120:
            return hit[0]
        for resolver in self.resolvers:
            try:
                ips = await asyncio.to_thread(_dns_query, host, resolver, self.out_device)
            except OSError:
                continue
            for ip in ips:
                if not is_local(ip):
                    self._resolved[host] = (ip, time.monotonic())
                    return ip
        return None

    async def _pump(self, reader, writer, counter):
        try:
            while True:
                chunk = await reader.read(65536)
                if not chunk:
                    break
                self.stats[counter] += len(chunk)
                writer.write(chunk)
                await writer.drain()
        except OSError:
            pass
        finally:
            writer.close()

    async def _handle(self, port, client_r, client_w):
        peer = client_w.get_extra_info("peername")
        peer_ip = peer[0] if peer else "?"
        up_w = None
        try:
            first = await asyncio.wait_for(client_r.read(4096), 15)
            name = parse_sni(first)
            if name not in self.allow:
                self.stats["refused"] += 1
                return
            ip = await self.resolve(name)
            if not ip:
                self.stats["resolve_fail"] += 1
                log.warning("relay: can't resolve %s", name)
                return
            try:
                up_r, up_w = await asyncio.wait_for(
                    asyncio.open_connection(ip, port), CONNECT_TIMEOUT)
            except OSError as e:
                self.stats["dial_fail"] += 1
                log.warning("relay: can't reach %s (%s:%d): %s", name, ip, port, e)
                return
            up_w.write(first)
            await up_w.drain()
            self.stats["bytes_up"] += len(first)
            self.stats["forwarded"] += 1
            self.stats["open"] += 1
            self.bulbs.add(peer_ip)
            await asyncio.gather(self._pump(client_r, up_w, "bytes_up"),
                                 self._pump(up_r, client_w, "bytes_down"))
        except (OSError, asyncio.TimeoutError):
            pass
        finally:
            self.stats["open"] = max(0, self.stats["open"] - (1 if up_w else 0))
            client_w.close()

    async def start(self):
        for port in self.ports:
            srv = await asyncio.start_server(
                lambda r, w, p=port: self._handle(p, r, w), self.listen_ip, port)
            self.servers.append(srv)
        self.started = time.monotonic()
        log.info("cloud relay on %s ports %s -> %s (forwarding %s)",
                 self.listen_ip, ",".join(map(str, self.ports)), self.resolvers, ", ".join(sorted(self.allow)))

    async def stop(self):
        for srv in self.servers:
            srv.close()
        self.servers = []

    def status(self):
        return {"listening": bool(self.servers), "listen_ip": self.listen_ip,
                "allow": sorted(self.allow), "bulbs": len(self.bulbs), **self.stats}
