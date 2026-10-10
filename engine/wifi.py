"""Ask the root WiFi helper (engine/wifi_helper.py) to check a network name and
password. See deploy/dmx_smartbulb-wifi.socket."""

import asyncio
import json

SOCKET = "/run/dmx_smartbulb-wifi.sock"
TIMEOUT_S = 75        # a scan plus the handshake attempt; the helper gives up first


async def check(ssid, password, path=SOCKET):
    """-> {"result": ok|wrong_password|not_found|open|invalid|unavailable|error, "message": str}"""
    try:
        reader, writer = await asyncio.open_unix_connection(path)
    except OSError:
        return {"result": "unavailable",
                "message": "The WiFi check isn't installed on this Pi (run sudo ./setup.sh), so the password wasn't checked."}
    try:
        writer.write(json.dumps({"ssid": ssid, "password": password}).encode() + b"\n")
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), TIMEOUT_S)
        reply = json.loads(line)
        return {"result": str(reply.get("result", "error")), "message": str(reply.get("message", ""))}
    except (OSError, ValueError, asyncio.TimeoutError) as e:
        return {"result": "error", "message": f"The WiFi check didn't finish: {e or 'timed out'}"}
    finally:
        writer.close()
