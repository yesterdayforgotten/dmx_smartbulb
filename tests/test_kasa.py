import asyncio

from engine import kasa
from tools.fake_bulbs import FakeBulbFleet

# Port for the fake bulbs in tests, so a real 9999 listener can't interfere.
PORT = 19999


def test_encrypt_roundtrip_and_known_bytes():
    msg = '{"system":{"get_sysinfo":null}}'
    enc = kasa.encrypt(msg)
    assert kasa.decrypt(enc) == msg
    # First byte is '{' (0x7b) XOR the initial key 171.
    assert enc[0] == 0x7B ^ 171
    # Same bytes as the old Django-era implementation.
    key, legacy = 171, bytearray(msg.encode())
    for i in range(len(legacy)):
        key ^= legacy[i]
        legacy[i] = key
    assert enc == bytes(legacy)


def test_scale_hsv():
    assert kasa.scale_hsv(255, 255, 255) == (360, 100, 100)
    assert kasa.scale_hsv(0, 0, 255)[0] == 180          # no saturation: hue forced to 180
    for v in (0, 1, 2):
        assert kasa.scale_hsv(10, 10, v)[2] == 0        # 0-2 turn the bulb off
    assert kasa.scale_hsv(10, 10, 3)[2] == 1


def test_light_state_payload():
    cmd = kasa.light_state(120, 50, 0, 30)["smartlife.iot.smartbulb.lightingservice"]["transition_light_state"]
    assert cmd["on_off"] == 0 and cmd["transition_period"] == 30 and cmd["color_temp"] == 0
    assert b" " not in kasa.decrypt(kasa.pack(kasa.light_state(1, 2, 3, 4))).encode()  # compact


def test_sysinfo_mac():
    assert kasa.sysinfo_mac({"mic_mac": "50c7bf123456"}) == "50C7BF123456"
    assert kasa.sysinfo_mac({"mac": "50:C7:BF:12:34:56"}) == "50C7BF123456"
    assert kasa.sysinfo_mac({}) is None


def run(coro):
    return asyncio.run(coro)


def test_transport_commands_and_replies():
    async def go():
        fleet = await FakeBulbFleet(3, port=PORT).start()
        replies = []
        t = await kasa.KasaTransport.create(lambda ip, r, ts: replies.append((ip, r)), port=PORT)
        try:
            for ip in fleet.ips:
                t.send(ip, kasa.light_state(240, 100, 50, 30))
            await asyncio.sleep(0.2)
            assert {ip for ip, _ in replies} == set(fleet.ips)
            assert fleet.bulbs[0].state["hue"] == 240 and fleet.bulbs[0].commands == 1
            info = await t.request(fleet.ips[1], kasa.SYSINFO)
            assert kasa.sysinfo_mac(info["system"]["get_sysinfo"]) == fleet.bulbs[1].mac
        finally:
            t.close()
            fleet.stop()
    run(go())


def test_request_times_out_on_offline_bulb():
    async def go():
        fleet = await FakeBulbFleet(1, port=PORT).start()
        fleet.bulbs[0].online = False
        t = await kasa.KasaTransport.create(port=PORT)
        try:
            assert await t.request(fleet.ips[0], kasa.SYSINFO, timeout=0.2) is None
        finally:
            t.close()
            fleet.stop()
    run(go())


def test_discover_unicast_targets():
    async def go():
        fleet = await FakeBulbFleet(4, port=PORT).start()
        fleet.bulbs[2].online = False
        try:
            found = await kasa.discover(fleet.ips, port=PORT, timeout=0.3)
            assert set(found) == set(fleet.ips) - {fleet.ips[2]}
            assert kasa.sysinfo_mac(found[fleet.ips[0]]) == fleet.bulbs[0].mac
        finally:
            fleet.stop()
    run(go())


def test_preferred_state_accepted():
    async def go():
        fleet = await FakeBulbFleet(1, port=PORT).start()
        t = await kasa.KasaTransport.create(port=PORT)
        try:
            r = await t.request(fleet.ips[0], kasa.preferred_state(0, 0, 80, 2700))
            assert r[kasa.LIGHTING]["set_preferred_state"]["err_code"] == 0
            assert fleet.bulbs[0].preferred["brightness"] == 80
        finally:
            t.close()
            fleet.stop()
    run(go())
