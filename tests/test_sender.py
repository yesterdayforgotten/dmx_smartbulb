import pytest

from engine.config import validate
from engine.sender import Sender, apply_curve, change_size, dmx_to_state

A, B, C = "50C7BF000001", "50C7BF000002", "50C7BF000003"
LIGHT = "smartlife.iot.smartbulb.lightingservice"


def make(bulbs=None, groups=None, **sender):
    # Most tests here exercise the priority scheduler and adaptive fades; the
    # sync-mode tests ask for mode="sync" explicitly.
    sender.setdefault("mode", "priority")
    sender.setdefault("adaptive_transition", True)
    cfg = validate({
        "bulbs": bulbs or {A: {"name": "a", "ip": "10.0.0.1", "channel": 1},
                           B: {"name": "b", "ip": "10.0.0.2", "channel": 1},   # shares a's channels
                           C: {"name": "c", "ip": "10.0.0.3", "channel": 4}},
        "groups": groups or {},
        "sender": sender,
    })
    sent = []
    s = Sender(cfg, lambda ip, cmd: sent.append((ip, cmd)) or True)
    return s, sent


def frame(**ch):
    d = bytearray(512)
    for k, v in ch.items():
        d[int(k[1:]) - 1] = v
    return bytes(d)


def state_of(cmd):
    return cmd[LIGHT]["transition_light_state"]


def echo(cmd):
    """What a KL bulb replies: the state it just set."""
    return {LIGHT: {"transition_light_state": dict(state_of(cmd), err_code=0)}}


def test_curve():
    assert [apply_curve(v, "linear") for v in (0, 2, 3, 255)] == [0, 0, 1, 100]
    assert apply_curve(128, "square") < apply_curve(128, "linear")
    assert apply_curve(255, "scurve") == 100 and apply_curve(3, "square") == 1


def test_shared_channel_bulbs_update_together():
    s, sent = make()
    s.update_dmx(frame(c1=0, c2=255, c3=255), 0.0, 0.0)
    s.tick(0.0)
    assert {ip for ip, _ in sent} == {"10.0.0.1", "10.0.0.2", "10.0.0.3"}
    sent.clear()
    s.update_dmx(frame(c1=100, c2=255, c3=255), 0.1, 0.1)
    s.tick(0.1)
    assert {ip for ip, _ in sent} == {"10.0.0.1", "10.0.0.2"}    # both sharers, not c


def test_per_bulb_rate_limit():
    s, sent = make(min_interval_ms=100)
    t = 0.0
    for i in range(50):              # a fast fade: a new value every 10 ms for 0.5 s
        s.update_dmx(frame(c1=i * 5, c2=255, c3=255), t, t)
        s.tick(t)
        t += 0.01
    to_a = [c for ip, c in sent if ip == "10.0.0.1"]
    assert 5 <= len(to_a) <= 6       # 100 ms interval over 0.5 s
    assert state_of(to_a[-1])["hue"] != 0   # and it keeps tracking the fade


def test_global_budget_and_largest_change_first():
    bulbs = {f"50C7BF0000{i:02X}": {"ip": f"10.0.1.{i}", "channel": 1 + 3 * i} for i in range(30)}
    s, sent = make(bulbs, budget_pps=100, refresh_s=60)
    data = bytearray(512)
    for i in range(30):
        data[3 * i + 2] = 50
    s.update_dmx(bytes(data), 0.0, 0.0)
    t = 0.0
    while t < 1.0:                                  # let every bulb get its first command
        s.tick(t)
        t += 0.01
    assert all(not rt.dirty for rt in s.bulbs.values())
    sent.clear()
    for i in range(30):
        data[3 * i + 2] = 255 if i == 7 else 60     # bulb 7 gets by far the biggest change
    s.update_dmx(bytes(data), t, t)
    s.tokens = 0
    s.last_tick = t
    t += 0.05
    n = s.tick(t)
    assert n == 5                                   # 100 pkt/s for 50 ms
    assert sent[0][0] == "10.0.1.7"
    total = n
    while t < 2.0:
        t += 0.01
        total += s.tick(t)
    assert total == 30                              # everyone served, nobody starved
    assert s.stats["budget_waits"] > 0


def test_confirmed_bulbs_are_not_refreshed():
    s, sent = make(refresh_s=2.0)
    s.update_dmx(frame(c3=200), 0.0, 0.0)
    s.tick(0.0)
    for ip, cmd in sent:
        if ip == "10.0.0.1":
            s.on_reply(ip, echo(cmd), 0.01)          # only a confirms
    sent.clear()
    s.tick(2.05)
    assert sorted(ip for ip, _ in sent) == ["10.0.0.2", "10.0.0.3"]   # b and c never confirmed


def test_refresh_resends_after_refresh_s():
    s, sent = make(refresh_s=2.0)
    s.update_dmx(frame(c3=200), 0.0, 0.0)
    s.tick(0.0)
    sent.clear()
    s.tick(1.0)
    assert sent == []
    s.tick(2.05)
    assert len(sent) == 3 and s.stats["refreshes"] == 3
    assert state_of(sent[0][1])["transition_period"] == 0   # a refresh doesn't fade


def test_adaptive_transition():
    s, sent = make(min_interval_ms=50, snap_threshold=0.15)
    s.update_dmx(frame(c1=0, c2=255, c3=255), 0.0, 0.0)
    s.tick(0.0)
    sent.clear()
    s.update_dmx(frame(c1=2, c2=255, c3=255), 0.1, 0.1)      # tiny hue step: fade
    s.tick(0.1)
    assert state_of(sent[0][1])["transition_period"] == 50
    sent.clear()
    s.update_dmx(frame(c1=128, c2=255, c3=255), 0.2, 0.2)    # big jump: snap
    s.tick(0.2)
    assert state_of(sent[0][1])["transition_period"] == 0


def test_fixed_transition_when_adaptive_off():
    s, sent = make(adaptive_transition=False, fixed_transition_ms=30)
    s.update_dmx(frame(c3=255), 0.0, 0.0)
    s.tick(0.0)
    assert all(state_of(c)["transition_period"] == 30 for _, c in sent)


def test_dmx_wins_over_manual():
    s, sent = make()
    s.update_dmx(frame(c1=10, c2=255, c3=255), 0.0, 0.0)
    s.tick(0.0)
    s.set_manual(C, ("temp", 2700, 80), 0.1)
    s.tick(0.1)
    assert s.bulbs[C].target == ("temp", 2700, 80)
    s.update_dmx(frame(c1=20, c2=255, c3=255), 0.2, 0.2)    # DMX changed only for a/b
    s.tick(0.2)
    assert s.bulbs[C].source == "manual"                     # c's channels didn't move
    s.update_dmx(frame(c1=20, c2=255, c3=255, c6=255), 0.3, 0.3)   # now c's channels move
    s.tick(0.3)
    assert s.bulbs[C].source == "dmx" and s.bulbs[C].target[0] == "hsv"


def test_dmx_toggle_off_keeps_manual():
    bulbs = {A: {"ip": "10.0.0.1", "channel": 1, "dmx": False}}
    s, sent = make(bulbs)
    s.set_manual(A, ("hsv", 120, 100, 50), 0.0)
    s.tick(0.0)
    s.update_dmx(frame(c1=200, c2=200, c3=200), 0.1, 0.1)
    s.tick(0.5)
    assert s.bulbs[A].target == ("hsv", 120, 100, 50)


def test_look_recall_then_dmx_returns():
    s, _ = make()
    s.update_dmx(frame(c3=255), 0.0, 0.0)
    s.apply_look({A: {"k": 3000, "v": 60}, C: {"h": 10, "s": 50, "v": 40}}, 0.1)
    assert s.bulbs[A].source == "look" and s.bulbs[B].source == "dmx"
    s.update_dmx(frame(c3=255), 0.2, 0.2)
    assert s.bulbs[A].source == "look"
    s.update_dmx(frame(c3=100), 0.3, 0.3)
    assert s.bulbs[A].source == "dmx"


def test_backoff_on_missed_replies_and_recovery():
    s, sent = make(min_interval_ms=50, max_backoff_ms=800)
    t = 0.0
    for i in range(60):                       # 3 s of changes, no replies at all
        s.update_dmx(frame(c1=i, c2=255, c3=255), t, t)
        s.tick(t)
        t += 0.05
    a = s.bulbs[A]
    assert a.interval == pytest.approx(0.8) and a.backed_off(s.min_interval)
    assert not a.online(t)
    i = 0
    while a.interval > s.min_interval and i < 400:   # replies come back while it keeps sending
        s.update_dmx(frame(c1=(100 + i) & 0xFF, c2=255, c3=255), t, t)
        before = len(sent)
        s.tick(t)
        for ip, cmd in sent[before:]:
            if ip == "10.0.0.1":
                s.on_reply(ip, echo(cmd), t + 0.01)
        t += 0.05
        i += 1
    assert a.interval == pytest.approx(0.05) and a.online(t)
    assert a.rtt == pytest.approx(0.01)


def test_channel_510_works():
    s, sent = make({A: {"ip": "10.0.0.1", "channel": 510}})
    d = bytearray(512)
    d[509:512] = bytes([0, 0, 255])
    s.update_dmx(bytes(d), 0.0, 0.0)
    s.tick(0.0)
    assert state_of(sent[0][1])["brightness"] == 100


def test_follow_group_channel():
    s, sent = make({A: {"ip": "10.0.0.1", "follow": "G"}, B: {"ip": "10.0.0.2", "channel": 1}},
                   groups={"G": {"channel": 100}})
    s.update_dmx(frame(c102=255), 0.0, 0.0)
    s.tick(0.0)
    assert [ip for ip, _ in sent] == ["10.0.0.1"] or state_of(dict(sent)["10.0.0.1"])["brightness"] == 100


def test_blackout_on_dmx_loss_and_resume():
    s, _ = make()
    s.update_dmx(frame(c3=255, c6=255), 0.0, 0.0)
    s.tick(0.0)
    s.dmx_lost("blackout", 6.0)
    assert s.bulbs[A].target[3] == 0 and s.bulbs[A].source == "loss"
    s.update_dmx(frame(c3=255, c6=255), 7.0, 7.0)
    assert s.bulbs[A].source == "dmx" and s.bulbs[A].target[3] == 100


def test_change_size():
    assert change_size(("hsv", 0, 100, 100), ("hsv", 0, 100, 100)) == 0
    assert change_size(("hsv", 350, 100, 100), ("hsv", 10, 100, 100)) == pytest.approx(20 / 180)
    assert change_size(("hsv", 0, 0, 0), ("temp", 2700, 50)) == 1.0
    assert dmx_to_state((0, 0, 255), "linear") == ("hsv", 180, 0, 100)


def test_small_changes_are_not_starved_by_big_ones():
    """A dim bulb whose hue creeps every frame must still get its share of the
    budget while bright bulbs change a lot (the waiting bonus must build up)."""
    bulbs = {f"50C7BF0000{i:02X}": {"ip": f"10.0.1.{i}", "channel": 1 + 3 * i} for i in range(20)}
    s, sent = make(bulbs, budget_pps=100)
    replies = []
    s.send = lambda ip, cmd: sent.append(ip) or replies.append((ip, cmd)) or True
    t = 0.0
    while t < 10:
        for ip, cmd in replies:
            s.on_reply(ip, echo(cmd), t)
        replies.clear()
        d = bytearray(512)
        k = int(t * 31)
        for i in range(20):
            if i == 0:
                d[0:3] = bytes([k & 0xFF, 20, 20])             # dim, hue creeping: tiny changes
            else:
                d[3 * i:3 * i + 3] = bytes([(k * 7) & 0xFF, 255, (k * 5) & 0xFF])  # big changes
        s.update_dmx(bytes(d), t, t)
        s.tick(t)
        t += 0.01
    rate_dim = sent.count("10.0.1.0") / 10
    fair = 100 / 20
    assert rate_dim > fair * 0.6, f"dim bulb got {rate_dim}/s, fair share {fair}/s"


def test_occasional_loss_does_not_back_off():
    """1-3% loss is normal on WiFi; a single miss must not halve a bulb's rate."""
    s, sent = make(min_interval_ms=100)
    t, n = 0.0, 0
    while t < 20:
        s.update_dmx(frame(c1=int(t * 31) & 0xFF, c2=255, c3=255), t, t)
        before = len(sent)
        s.tick(t)
        for ip, cmd in sent[before:]:
            if ip != "10.0.0.1":
                continue
            n += 1
            if n % 50 != 0:                          # lose 2% of a's replies
                s.on_reply(ip, echo(cmd), t + 0.01)
        t += 0.02
    a = s.bulbs[A]
    assert a.misses == pytest.approx(n / 50, abs=1)  # every lost reply is counted, exactly
    assert not a.backed_off(s.min_interval)
    assert a.rtt == pytest.approx(0.01)               # and RTT isn't skewed by the losses



def _fade_run(mode, n=30, budget=100, seconds=6):
    bulbs = {f"50C7BF0000{i:02X}": {"ip": f"10.0.1.{i}", "channel": 1 + 3 * i} for i in range(n)}
    s, sent = make(bulbs, budget_pps=budget, min_interval_ms=100, mode=mode, refresh_s=60)
    sends = []
    replies = []
    s.send = lambda ip, cmd: sends.append((ip, cmd)) or replies.append((ip, cmd)) or True
    t = 0.0
    while t < seconds:
        for ip, cmd in replies:
            s.on_reply(ip, echo(cmd), t + 0.005)
        replies.clear()
        d = bytearray(512)
        k = int(t * 31)
        for i in range(n):
            d[3 * i:3 * i + 3] = bytes([k & 0xFF, 255, 200])     # every bulb fades the same way
        s.update_dmx(bytes(d), t, t)
        s.tick(t)
        t += 0.01
    s._housekeep_metrics(t + 10)
    return s, sends


def test_sync_mode_sends_all_changed_bulbs_together():
    s, sends = _fade_run("sync")
    from engine.sender import percentile
    assert s.frame_period == pytest.approx(0.3)                   # 30 bulbs / 100 pkt/s
    # Each output frame carries all 30 bulbs with one shared transition.
    first_frame = sends[:30]
    assert len({ip for ip, _ in first_frame}) == 30
    later = [state_of(c)["transition_period"] for _, c in sends[30:60]]
    assert set(later) <= {300, 0}
    assert len(sends) / 6 <= 100 * 1.05                           # stays within the budget
    assert percentile(s.delivery_spread, 0.95) < 0.02             # all bulbs get each change together


def test_priority_mode_spreads_deliveries():
    s, _ = _fade_run("priority")
    from engine.sender import percentile
    assert percentile(s.delivery_spread, 0.95) > 0.1              # the budget trickles changes out


def test_sync_mode_skips_backed_off_bulb():
    s, sends = _fade_run("sync", n=4, budget=100, seconds=1)
    a = s.bulbs["50C7BF000000"]
    a.interval = 2.0
    a.last_send = 100.0
    s.frame_next = 0
    s.update_dmx(bytes([9, 255, 200] * 4) + bytes(500), 100.1, 100.1)
    before = len(sends)
    s.tick(100.1)
    assert "10.0.1.0" not in [ip for ip, _ in sends[before:]]



def test_sync_mode_keeps_full_rate_when_interval_equals_frame_period():
    """min interval 66 ms and 15 bulbs at 225 pkt/s give a 66.7 ms frame; with
    10 ms ticks a bulb must still be sent every frame (15 Hz), not every other."""
    n = 15
    bulbs = {f"50C7BF0000{i:02X}": {"ip": f"10.0.1.{i}", "channel": 1 + 3 * i} for i in range(n)}
    s, sent = make(bulbs, budget_pps=225, min_interval_ms=66, mode="sync", refresh_s=60)
    sends, replies = [], []
    s.send = lambda ip, cmd: sends.append(ip) or replies.append((ip, cmd)) or True
    t = 0.0
    while t < 10:
        for ip, cmd in replies:
            s.on_reply(ip, echo(cmd), t + 0.005)
        replies.clear()
        d = bytearray(512)
        for i in range(n):
            d[3 * i:3 * i + 3] = bytes([int(t * 31) & 0xFF, 255, 200])
        s.update_dmx(bytes(d), t, t)
        s.tick(t)
        t += 0.0103                     # ticks that don't line up with the frame
    rate = sends.count("10.0.1.0") / 10
    assert rate == pytest.approx(15, abs=0.5)



def test_poll_mismatch_triggers_restore():
    """A confirmed bulb that is found showing something else (power blip) is
    re-sent its state on the next refresh."""
    s, sent = make(refresh_s=2.0)
    s.update_dmx(frame(c3=200), 0.0, 0.0)
    s.tick(0.0)
    for ip, cmd in sent:
        s.on_reply(ip, echo(cmd), 0.01)
    assert s.bulbs[A].confirmed
    # A poll shows bulb a back on a warm-white power-on default.
    s.on_reply("10.0.0.1", {LIGHT: {"get_light_state": {"on_off": 1, "color_temp": 2700, "brightness": 80,
                                                        "hue": 0, "saturation": 0, "err_code": 0}}}, 1.0)
    assert not s.bulbs[A].confirmed
    sent.clear()
    s.tick(2.05)
    assert [ip for ip, _ in sent] == ["10.0.0.1"]
    # A poll that matches what was sent leaves a confirmed bulb alone.
    b = s.bulbs[B]
    h, sat, v = b.sent[1], b.sent[2], b.sent[3]
    s.on_reply("10.0.0.2", {LIGHT: {"get_light_state": {"on_off": 1, "hue": 180 if sat == 0 else h, "saturation": sat,
                                                        "brightness": v, "color_temp": 0, "err_code": 0}}}, 1.0)
    assert b.confirmed



def test_one_lost_idle_check_does_not_flag_offline():
    s, _ = make(idle_check_s=3.0)
    a = s.bulbs[A]
    s.on_reply("10.0.0.1", {LIGHT: {"get_light_state": {"on_off": 1, "hue": 0, "saturation": 0,
                                                        "brightness": 50, "color_temp": 2700}}}, 0.0)
    # The check at 3 s is lost; at 6.5 s (just before the next one answers) it's still online.
    assert s.bulb_status(6.5)[A]["online"]
    assert not s.bulb_status(8.0)[A]["online"]       # two checks missed: offline


def test_hsic_white_at_saturation_zero():
    from engine.sender import cct_to_kelvin
    assert dmx_to_state((10, 0, 255, 0), "linear") == ("temp", 2500, 100)
    assert dmx_to_state((10, 0, 255, 255), "linear") == ("temp", 6500, 100)
    assert dmx_to_state((10, 2, 128, 128), "linear")[0] == "temp"        # sat byte <= 2 rounds to 0%
    assert dmx_to_state((0, 255, 255, 128), "linear") == ("hsv", 0, 100, 100)   # colored: CCT ignored
    assert dmx_to_state((10, 0, 255), "linear")[0] == "hsv"              # HSI bulbs never go white
    assert cct_to_kelvin(128) == 4510


def test_hsic_bulb_reads_fourth_channel():
    s, sent = make()
    mac = next(iter(s.bulbs))
    s.bulbs[mac].size = 4
    data = bytearray(512)
    data[0:4] = bytes((0, 0, 255, 255))
    s.update_dmx(bytes(data), 1.0, 1.0)
    assert s.bulbs[mac].target == ("temp", 6500, 100)
