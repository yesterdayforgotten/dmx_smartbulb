"""Ask the root WiFi helper (engine/wifi_helper.py) to use the Pi's radio: check a
network name and password, list bulbs in setup mode, or pair one. See
deploy/dmx_smartbulb-wifi.socket."""

import asyncio
import json

SOCKET = "/run/dmx_smartbulb-wifi.sock"
TIMEOUT_S = 75        # a scan plus a join or handshake attempt; the helper gives up first


async def _ask(request, path):
    try:
        reader, writer = await asyncio.open_unix_connection(path)
    except OSError:
        return {"result": "unavailable",
                "message": "The WiFi helper isn't installed on this Pi (run sudo ./setup.sh)."}
    try:
        writer.write(json.dumps(request).encode() + b"\n")
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), TIMEOUT_S)
        reply = json.loads(line)
        return {**reply, "result": str(reply.get("result", "error")), "message": str(reply.get("message", ""))}
    except (OSError, ValueError, asyncio.TimeoutError) as e:
        return {"result": "error", "message": f"The WiFi helper didn't finish: {e or 'timed out'}"}
    finally:
        writer.close()


async def check(ssid, password, path=SOCKET):
    """-> {"result": ok|wrong_password|not_found|open|invalid|unavailable|error, "message": str}"""
    return await _ask({"ssid": ssid, "password": password}, path)


async def scan_setup(path=SOCKET):
    """-> {"result", "message", "networks": [bulbs' setup network names]}"""
    return await _ask({"op": "scan_setup"}, path)


async def pair(setup_ssid, ssid, password, alias="", path=SOCKET):
    """-> {"result": sent|not_found|invalid|unavailable|error, "message", "mac", "model", "fw"}"""
    return await _ask({"op": "pair", "setup_ssid": setup_ssid, "ssid": ssid, "password": password, "alias": alias}, path)


async def venue_status(path=SOCKET):
    return await _ask({"op": "venue_status"}, path)


async def venue_networks(path=SOCKET):
    return await _ask({"op": "venue_networks"}, path)


async def venue_join(ssid, password, path=SOCKET):
    return await _ask({"op": "venue_join", "ssid": ssid, "password": password}, path)


async def venue_leave(path=SOCKET):
    return await _ask({"op": "venue_leave"}, path)


async def boot_reset(path=SOCKET):
    return await _ask({"op": "boot_reset"}, path)
