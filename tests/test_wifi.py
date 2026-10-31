"""The WiFi password check: the root helper's parsing, the engine client, the endpoint."""

import asyncio
import json
import os
import socket
import threading

import pytest

from engine import kasa, wifi, wifi_helper

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


def test_handshake_waits_for_slow_wpa_supplicant_output(tmp_path, monkeypatch):
    fake = tmp_path / "wpa_supplicant"
    fake.write_text("#!/bin/sh\nsleep 0.3\necho 'wlan0: Trying to associate'\nsleep 0.3\n"
                    "echo 'wlan0: WPA: 4-Way Handshake failed - pre-shared key may be incorrect'\nsleep 30\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path}:{os.environ['PATH']}")
    monkeypatch.setattr(wifi_helper, "CONF", str(tmp_path / "wifi.conf"))
    assert wifi_helper.handshake("PHDMXLTS", "hunter22") == "wrong_password"
    assert not (tmp_path / "wifi.conf").exists()


def test_setup_networks_are_recognized():
    for name in ("TP-LINK_Smart Bulb_A86D", "Kasa Smart Bulb 7B"):
        assert wifi_helper.SETUP_SSID.match(name)
    assert not wifi_helper.SETUP_SSID.match("PHDMXLTS")
    assert wifi_helper.pair("PHDMXLTS", "Show", "hunter22")["result"] == "invalid"
    assert wifi_helper.pair("TP-LINK_Smart Bulb_A86D", "", "hunter22")["result"] == "invalid"
    assert wifi_helper.pair("TP-LINK_Smart Bulb_A86D", "Show", "short")["result"] == "invalid"


class SetupBulb(threading.Thread):
    """A bulb on its setup network; goes silent after the WiFi settings, as a real one may."""

    def __init__(self, sysinfo, silent_after_join=True, join_reply=None):
        super().__init__(daemon=True)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.settimeout(3)
        self.addr = self.sock.getsockname()
        self.sysinfo, self.silent, self.join_reply = sysinfo, silent_after_join, join_reply
        self.seen = []

    def run(self):
        try:
            while True:
                data, addr = self.sock.recvfrom(4096)
                cmd = kasa.unpack(data)
                self.seen.append(cmd)
                if "system" in cmd:
                    self.sock.sendto(kasa.pack({"system": {"get_sysinfo": self.sysinfo}}), addr)
                elif "smartlife.iot.common.softaponboarding" in cmd:
                    if not self.silent:
                        self.sock.sendto(kasa.pack({"smartlife.iot.common.softaponboarding": {"set_stainfo": self.join_reply}}), addr)
                else:
                    self.sock.sendto(kasa.pack({"ok": 1}), addr)
        except OSError:
            pass


BULB = {"mic_type": "IOT.SMARTBULB", "model": "KL135(US)", "sw_ver": "1.0.15", "mic_mac": "5091E39BA86D"}


def client():
    return socket.socket(socket.AF_INET, socket.SOCK_DGRAM)


def test_onboard_names_the_bulb_and_sends_the_network():
    bulb = SetupBulb(BULB)
    bulb.start()
    with client() as sock:
        got = wifi_helper.onboard(sock, bulb.addr, "PHDMXLTS", "hunter22", "Stage left")
    assert got == {"mac": kasa.sysinfo_mac(BULB), "model": "KL135(US)", "fw": "1.0.15"}
    join = [c for c in bulb.seen if "smartlife.iot.common.softaponboarding" in c][0]
    assert join["smartlife.iot.common.softaponboarding"]["set_stainfo"] == {
        "ssid": "PHDMXLTS", "password": "hunter22", "key_type": 3}
    assert any("Stage left" in json.dumps(c) for c in bulb.seen)


def test_onboard_refuses_other_devices_and_reports_a_refusal():
    bulb = SetupBulb({"mic_type": "IOT.SMARTPLUGSWITCH", "model": "HS100"})
    bulb.start()
    with client() as sock, pytest.raises(RuntimeError, match="isn't a supported"):
        wifi_helper.onboard(sock, bulb.addr, "Show", "hunter22")
    assert not any("smartlife.iot.common.softaponboarding" in c for c in bulb.seen)
    bulb = SetupBulb(BULB, silent_after_join=False, join_reply={"err_code": -3, "err_msg": "bad"})
    bulb.start()
    with client() as sock, pytest.raises(RuntimeError, match="refused"):
        wifi_helper.onboard(sock, bulb.addr, "Show", "hunter22")


def test_onboard_open_network_uses_key_type_0():
    assert kasa.join_wifi("Open", "")["smartlife.iot.common.softaponboarding"]["set_stainfo"]["key_type"] == 0
