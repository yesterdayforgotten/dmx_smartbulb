"""The WiFi password check: the root helper's parsing, the engine client, the endpoint."""

import asyncio
import json
import os
import socket
import subprocess
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


# ---- venue WiFi: never run for real here, because it would take over the radio

class FakeRun:
    """Stands in for the helper's run(): records commands and answers from a table."""

    def __init__(self, up_fails=False, addrs="2: eth0    inet 192.168.20.100/24 brd x\n3: wlan0    inet 172.22.33.5/20 brd y\n"):
        self.cmds, self.up_fails, self.addrs = [], up_fails, addrs

    def __call__(self, *cmd, timeout=15):
        self.cmds.append(cmd)
        out, rc = "", 0
        if cmd[:4] == ("ip", "-4", "-o", "addr"):
            out = self.addrs
        elif "connection" in cmd and "up" in cmd and self.up_fails:
            rc = 1
        elif "connection" in cmd and "show" in cmd:
            out = "Wired:802-3-ethernet\n"
        elif "GENERAL.CONNECTION" in cmd:
            out = wifi_helper.VENUE_ID + "\n"
        elif cmd[-1] == "check":
            out = "full\n"
        return subprocess.CompletedProcess(cmd, rc, out, "secrets were wrong" if rc else "")


@pytest.fixture
def venue(tmp_path, monkeypatch):
    monkeypatch.setattr(wifi_helper, "VENUE_CONF", tmp_path / "conf" / "95.conf")
    monkeypatch.setattr(wifi_helper, "VENUE_PROFILE", tmp_path / "venue.nmconnection")
    monkeypatch.setattr(wifi_helper, "have_radio", lambda: True)
    monkeypatch.setattr(wifi_helper, "wifi_in_use", lambda: False)
    monkeypatch.setattr(wifi_helper.time, "sleep", lambda s: None)
    return tmp_path


def test_venue_join_keeps_the_password_off_command_lines_and_in_run_only(venue, monkeypatch):
    fake = FakeRun()
    monkeypatch.setattr(wifi_helper, "run", fake)
    r = wifi_helper.venue_join("Cafe Net", "hunter22")
    assert r["connected"] and r["ip"] == "172.22.33.5/20" and r["internet"] == "full" and r["temporary"]
    assert not any("hunter22" in " ".join(c) for c in fake.cmds)
    profile = wifi_helper.VENUE_PROFILE
    assert oct(profile.stat().st_mode)[-3:] == "600" and "psk=hunter22" in profile.read_text()
    assert "autoconnect=false" in profile.read_text() and "route-metric=50" in profile.read_text()
    assert "unmanaged-devices=\n" in wifi_helper.VENUE_CONF.read_text()


def test_venue_join_failure_undoes_everything(venue, monkeypatch):
    fake = FakeRun(up_fails=True)
    monkeypatch.setattr(wifi_helper, "run", fake)
    r = wifi_helper.venue_join("Cafe Net", "hunter22")
    assert r["result"] == "error" and "secrets were wrong" in r["message"]
    assert not wifi_helper.VENUE_PROFILE.exists() and not wifi_helper.VENUE_CONF.exists()
    assert ("rfkill", "block", "wifi") in fake.cmds


def test_venue_join_refuses_a_network_overlapping_the_bulbs(venue, monkeypatch):
    fake = FakeRun(addrs="2: eth0    inet 192.168.20.100/24 brd x\n3: wlan0    inet 192.168.20.7/24 brd y\n")
    monkeypatch.setattr(wifi_helper, "run", fake)
    r = wifi_helper.venue_join("Cafe Net", "hunter22")
    assert r["result"] == "error" and "overlap" in r["message"] and not wifi_helper.VENUE_PROFILE.exists()
    assert wifi_helper.subnet_clash("10.0.0.5/8", ["10.1.2.3/24"]) and not wifi_helper.subnet_clash("10.0.0.5/24", ["10.0.1.3/24"])


def test_radio_takeovers_refuse_while_wifi_is_in_use(venue, monkeypatch):
    monkeypatch.setattr(wifi_helper, "wifi_in_use", lambda: True)

    def forbidden(*cmd, timeout=15):
        raise AssertionError(f"touched the system: {cmd}")
    monkeypatch.setattr(wifi_helper, "run", forbidden)
    assert wifi_helper.check("ShowNet", "hunter22")[0] == "unavailable"
    assert wifi_helper.scan_setup()["result"] == "unavailable"
    assert wifi_helper.pair("TP-LINK_Smart Bulb_ABCD", "ShowNet", "hunter22")["result"] == "unavailable"
    assert wifi_helper.venue_join("Cafe Net", "hunter22")["result"] == "invalid"
    assert wifi_helper.boot_reset()["result"] == "ok"                  # and doesn't block the radio


def test_boot_reset_leaves_a_saved_profile_alone(venue, monkeypatch):
    fake = FakeRun()
    monkeypatch.setattr(wifi_helper, "run", fake)
    monkeypatch.setattr(wifi_helper, "wifi_profiles", lambda: ["Saved Net"])
    wifi_helper.boot_reset()
    assert ("rfkill", "block", "wifi") not in fake.cmds
    monkeypatch.setattr(wifi_helper, "wifi_profiles", lambda: [])
    wifi_helper.boot_reset()
    assert ("rfkill", "block", "wifi") in fake.cmds


def test_venue_keyfile_is_safe_with_odd_names():
    text = wifi_helper.venue_keyfile("Café;[x]", "back\\slash1")
    assert "ssid=67;97;102;195;169;59;91;120;93;\n" in text and "psk=back\\\\slash1" in text
    assert "wifi-security" not in wifi_helper.venue_keyfile("Open", "")
