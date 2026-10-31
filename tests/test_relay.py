"""The cloud relay: SNI parsing, loop-safe resolution, and transparent forwarding."""

import asyncio
import socket
import ssl
import struct

import pytest

from engine import relay
from engine.relay import CloudRelay, parse_sni, is_local


def client_hello(server_name):
    """A real TLS ClientHello for `server_name`, via SSLContext, to parse back."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    inc, out = ssl.MemoryBIO(), ssl.MemoryBIO()
    s = ctx.wrap_bio(inc, out, server_hostname=server_name)
    try:
        s.do_handshake()
    except ssl.SSLWantReadError:
        pass
    return out.read()


def test_parse_sni_reads_the_server_name():
    assert parse_sni(client_hello("n-devs.tplinkcloud.com")) == "n-devs.tplinkcloud.com"
    assert parse_sni(client_hello("n-deventry.tplinkcloud.com")) == "n-deventry.tplinkcloud.com"
    assert parse_sni(b"not a tls record at all") is None
    assert parse_sni(b"") is None


def test_is_local_rejects_private_and_loopback():
    for ip in ("192.168.10.100", "10.0.0.5", "127.0.0.1", "169.254.1.1", "0.0.0.0"):
        assert is_local(ip), ip
    for ip in ("34.199.90.22", "52.6.145.247", "1.1.1.1"):
        assert not is_local(ip), ip


def test_resolve_skips_a_loop_answer(monkeypatch):
    # The router would answer with the Pi's own LAN IP; the relay must reject it and
    # move to the next resolver, which gives the real public address.
    answers = {"1.1.1.1": ["192.168.10.100"], "8.8.8.8": ["34.199.90.22"]}
    monkeypatch.setattr(relay, "_dns_query", lambda host, res, device=None: answers[res])
    r = CloudRelay("192.168.10.100", resolvers=["1.1.1.1", "8.8.8.8"])
    assert asyncio.run(r.resolve("n-devs.tplinkcloud.com")) == "34.199.90.22"

    loop_only = {"1.1.1.1": ["192.168.10.100"], "8.8.8.8": ["10.1.2.3"]}
    monkeypatch.setattr(relay, "_dns_query", lambda host, res, device=None: loop_only[res])
    r2 = CloudRelay("192.168.10.100", resolvers=["1.1.1.1", "8.8.8.8"])
    assert asyncio.run(r2.resolve("n-devs.tplinkcloud.com")) is None


def test_relay_forwards_allowed_and_refuses_blocked(monkeypatch):
    async def go():
        # A fake "cloud" on 127.0.0.2 that upper-cases what it receives.
        async def upstream(r, w):
            data = await r.read(4096)
            w.write(data.upper())
            await w.drain()
            w.close()
        up = await asyncio.start_server(upstream, "127.0.0.2", 0)
        up_port = up.sockets[0].getsockname()[1]

        rel = CloudRelay("127.0.0.1", allow={"n-devs.tplinkcloud.com"}, ports=(up_port,))
        # resolve() returns the fake upstream's address; the relay dials (ip, same port).
        async def fake_resolve(host): return "127.0.0.2"
        monkeypatch.setattr(rel, "resolve", fake_resolve)
        await rel.start()

        # Allowed name: ClientHello (carries the SNI) then payload, echoed upper-cased.
        r, w = await asyncio.open_connection("127.0.0.1", up_port)
        w.write(client_hello("n-devs.tplinkcloud.com") + b"ping")
        await w.drain()
        got = await asyncio.wait_for(r.read(4096), 3)
        assert got.endswith(b"PING") and rel.stats["forwarded"] == 1
        w.close()

        # Blocked name: the relay closes without forwarding.
        r2, w2 = await asyncio.open_connection("127.0.0.1", up_port)
        w2.write(client_hello("n-deventry.tplinkcloud.com") + b"ping")
        await w2.drain()
        assert await asyncio.wait_for(r2.read(4096), 3) == b""
        assert rel.stats["refused"] == 1
        w2.close()
        await rel.stop()
        up.close()

    async def _coro(v):
        return v
    monkeypatch.setattr(relay, "_coro", _coro, raising=False)
    asyncio.run(go())


def test_config_relay_defaults_and_validation():
    from engine.config import validate
    c = validate({})
    assert c["relay"] == {"enabled": False, "ssid": "", "password": "",
                          "allow": ["n-devs.tplinkcloud.com"]}
    c = validate({"relay": {"enabled": "yes", "ssid": 5, "allow": []}})
    assert c["relay"]["enabled"] is False and c["relay"]["ssid"] == "" and c["relay"]["allow"] == ["n-devs.tplinkcloud.com"]
    c = validate({"relay": {"enabled": True, "ssid": "CafeWiFi", "password": "x", "allow": ["n-devs.tplinkcloud.com", "foo"]}})
    assert c["relay"]["enabled"] is True and c["relay"]["allow"] == ["n-devs.tplinkcloud.com", "foo"]
