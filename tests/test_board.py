import asyncio

from engine.board import SHOWS, Board, hsv_to_dmx


def test_hsv_to_dmx_matches_engine_scaling():
    from engine import kasa
    for h, s, v in [(0, 0, 0), (120, 100, 50), (300, 40, 100), (359, 100, 100)]:
        dmx = hsv_to_dmx(h, s, v)
        kh, ks, kv = kasa.scale_hsv(*dmx)
        if s:
            assert abs(kh - h) <= 2
        assert abs(ks - s) <= 1 and abs(kv - v) <= 1


def test_channels_and_fixture_color():
    b = Board(lambda: [("a", 1), ("b", 4)])
    b.set_channels({1: 10, 2: 300, 600: 5})
    assert b.values[0] == 10 and b.values[1] == 255
    b.set_fixture_color([4], 120, 100, 100)
    assert tuple(b.values[3:6]) == hsv_to_dmx(120, 100, 100)
    assert [f["channel"] for f in b.patched_values()] == [1, 4]
    b.blackout()
    assert not any(b.values)


def test_every_show_writes_values():
    async def go():
        b = Board(lambda: [("a", 1), ("b", 4), ("c", 7)])
        for name in SHOWS:
            b.blackout()
            b.start_show(name, speed=5)
            lit = False
            for _ in range(20):                       # some shows are dark half the time
                await asyncio.sleep(0.05)
                lit = lit or any(b.values[:9])
            assert b.show == name
            assert lit, name
            b.stop_show()
            assert b.show is None
    asyncio.run(go())


def test_shows_on_separate_fixtures_and_take_over():
    async def go():
        b = Board(lambda: [("a", 1), ("b", 4), ("c", 7), ("d", 10)])
        b.start_show("rainbow")                                   # all four
        b.start_show("flash", speed=2, target="G", channels=[7, 10])
        st = {r["target"]: r for r in b.status()["runs"]}
        assert st["all"]["fixtures"] == 2 and st["G"]["fixtures"] == 2 and st["G"]["speed"] == 2
        rid = st["G"]["id"]
        assert b.start_show("flash", speed=3, target="G", channels=[7, 10]) == rid   # speed only
        assert b.runs[rid]["speed"] == 3
        b.set_fixture_color([1], 0, 0, 50)                         # takes fixture a from rainbow
        assert b.runs[st["all"]["id"]]["channels"] == {4}
        b.set_channels({5: 0})                                     # fixture b: rainbow has nothing left
        assert [r["target"] for r in b.status()["runs"]] == ["G"]
        await asyncio.sleep(0.1)
        assert tuple(b.values[0:3]) == hsv_to_dmx(0, 0, 50)        # no show overwrote it
        b.start_show("breathe")                                    # all again: replaces everything
        assert [r["target"] for r in b.status()["runs"]] == ["all"]
        b.stop_show()
        assert not b.runs
    asyncio.run(go())
