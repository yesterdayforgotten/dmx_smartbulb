"""The WiFi password check: the root helper's parsing, the engine client, the endpoint."""

import asyncio
import json

from engine import wifi, wifi_helper

SCAN = """BSS 48:5b:39:e7:f3:32(on wlan0)
\tfreq: 2462
\tSSID: PHDMXLTS
\tCapability: ESS Privacy ShortSlotTime (0x0411)
\tRSN:\t * Version: 1
BSS 11:22:33:44:55:66(on wlan0)
\tSSID: Cafe Guest
\tCapability: ESS ShortSlotTime (0x0401)
"""


def test_parse_scan():
    assert wifi_helper.parse_scan(SCAN) == {"PHDMXLTS": True, "Cafe Guest": False}


def test_classify_wpa_supplicant_lines():
    assert wifi_helper.classify("wlan0: CTRL-EVENT-CONNECTED - Connection to 48:5b:39:e7:f3:32 completed") == "ok"
    assert wifi_helper.classify("wlan0: WPA: 4-Way Handshake failed - pre-shared key may be incorrect") == "wrong_password"
    assert wifi_helper.classify('wlan0: CTRL-EVENT-SSID-TEMP-DISABLED id=0 ssid="X" auth_failures=1 duration=10 reason=WRONG_KEY') == "wrong_password"
    assert wifi_helper.classify("wlan0: Trying to associate with 48:5b:39:e7:f3:32") is None


def test_check_rejects_impossible_passwords_without_the_radio():
    assert wifi_helper.check("PHDMXLTS", "short")[0] == "invalid"
    assert wifi_helper.check("", "longenough")[0] == "invalid"


def test_client_talks_to_the_helper_socket(tmp_path):
    path = str(tmp_path / "wifi.sock")
    seen = {}

    async def go():
        async def handle(reader, writer):
            seen.update(json.loads(await reader.readline()))
            writer.write(b'{"result": "wrong_password", "message": "rejected"}\n')
            await writer.drain()
            writer.close()
        server = await asyncio.start_unix_server(handle, path)
        async with server:
            return await wifi.check("PHDMXLTS", "hunter22", path=path)
    assert asyncio.run(go()) == {"result": "wrong_password", "message": "rejected"}
    assert seen == {"ssid": "PHDMXLTS", "password": "hunter22"}


def test_client_without_helper_says_unavailable(tmp_path):
    r = asyncio.run(wifi.check("X", "12345678", path=str(tmp_path / "missing.sock")))
    assert r["result"] == "unavailable"
