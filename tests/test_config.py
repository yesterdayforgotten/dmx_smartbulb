import json

import pytest

from engine.config import (ConfigError, ConfigStore, MAX_BULBS, bulb_channel, next_free_channel,
                           patch_conflicts, validate)

A, B, C = "50C7BF000001", "50C7BF000002", "50C7BF000003"


def bulbs(**kw):
    base = {A: {"name": "Left", "ip": "10.10.0.21", "channel": 1},
            B: {"name": "Right", "ip": "10.10.0.22", "channel": 4}}
    base.update(kw)
    return base


def test_defaults_fill_in():
    cfg = validate({"bulbs": bulbs()})
    assert cfg["sender"]["min_interval_ms"] == 33
    assert cfg["bulbs"][A]["dmx"] is True and cfg["bulbs"][A]["groups"] == []
    assert cfg["dmx_loss"]["mode"] == "hold"


def test_partial_settings_merge():
    cfg = validate({"sender": {"curve": "square"}})
    assert cfg["sender"]["curve"] == "square" and cfg["sender"]["budget_pps"] == 500


@pytest.mark.parametrize("ch", [0, 511, 512, 2.5, "1", True])
def test_bad_channels_rejected(ch):
    with pytest.raises(ConfigError, match="channel"):
        validate({"bulbs": bulbs(**{C: {"ip": "10.10.0.23", "channel": ch}})})


def test_channel_510_allowed():
    validate({"bulbs": bulbs(**{C: {"ip": "10.10.0.23", "channel": 510}})})


def test_bulb_limit():
    many = {f"{i:012X}": {"ip": None, "channel": None} for i in range(MAX_BULBS + 1)}
    with pytest.raises(ConfigError, match="at most 170"):
        validate({"bulbs": many})


def test_bad_mac_and_ip_and_duplicate_ip():
    with pytest.raises(ConfigError) as e:
        validate({"bulbs": {"nope": {"ip": "10.10.0.300"},
                            A: {"ip": "10.10.0.5"}, B: {"ip": "10.10.0.5"}}})
    text = str(e.value)
    assert "isn't a MAC" in text and "isn't an IPv4" in text and "same IP" in text


def test_follow_group():
    cfg = validate({"groups": {"Uplights": {"channel": 100}},
                    "bulbs": bulbs(**{C: {"ip": "10.10.0.23", "follow": "Uplights"}})})
    assert bulb_channel(cfg, C) == 100
    with pytest.raises(ConfigError, match="doesn't exist"):
        validate({"bulbs": {C: {"follow": "Nope"}}})
    with pytest.raises(ConfigError, match="not both"):
        validate({"groups": {"G": {}}, "bulbs": {C: {"follow": "G", "channel": 5}}})


def test_patch_conflicts():
    cfg = validate({"groups": {"G": {"channel": 7}},
                    "bulbs": {A: {"ip": "10.0.0.1", "channel": 1, "name": "a"},
                              B: {"ip": "10.0.0.2", "channel": 3, "name": "b"},     # overlaps a on 3
                              C: {"ip": "10.0.0.3", "follow": "G", "name": "c"},
                              "50C7BF000004": {"ip": "10.0.0.4", "follow": "G", "name": "d"}}})
    w = patch_conflicts(cfg)
    assert w == ["Overlap: channel 3 is used by a, b"]   # c and d share group G on purpose


def test_next_free_channel():
    cfg = validate({"groups": {"G": {"channel": 7}}, "bulbs": bulbs()})  # uses 1-6, group 7-9
    assert next_free_channel(cfg) == 10
    assert next_free_channel(cfg, start=2) == 10


def test_looks_and_loss_validation():
    cfg = validate({"bulbs": bulbs(), "looks": {"House": {A: {"k": 2700, "v": 80}, B: {"h": 30, "s": 90, "v": 50}}},
                    "dmx_loss": {"mode": "look", "look": "House"}})
    assert cfg["dmx_loss"]["look"] == "House"
    with pytest.raises(ConfigError, match="unknown bulb"):
        validate({"looks": {"X": {C: {"k": 3000, "v": 10}}}})
    with pytest.raises(ConfigError, match="DMX-loss look"):
        validate({"dmx_loss": {"mode": "look", "look": "Missing"}})


def test_store_roundtrip_and_alternation(tmp_path):
    store = ConfigStore(tmp_path / "config.json")
    assert store.load()["bulbs"] == {}                    # first run: defaults
    store.save({"bulbs": bulbs()})
    store.save({"bulbs": bulbs(), "sender": {"curve": "scurve"}})
    assert (tmp_path / "config.json").exists() and (tmp_path / "config.json.bak").exists()
    fresh = ConfigStore(tmp_path / "config.json")
    assert fresh.load()["sender"]["curve"] == "scurve" and fresh.seq == 2


def test_store_survives_damaged_newest_copy(tmp_path):
    store = ConfigStore(tmp_path / "config.json")
    store.save({"bulbs": bulbs()})                        # seq 1
    store.save({"bulbs": bulbs(), "web_port": 8080})      # seq 2
    # Find the newest copy and damage it as a power cut mid-write might.
    for p in (tmp_path / "config.json", tmp_path / "config.json.bak"):
        env = json.loads(p.read_text())
        if env["seq"] == 2:
            p.write_text(p.read_text()[:40])
    cfg = ConfigStore(tmp_path / "config.json").load()
    assert cfg["web_port"] == 80 and len(cfg["bulbs"]) == 2   # the previous save


def test_store_checksum_mismatch_is_ignored(tmp_path):
    store = ConfigStore(tmp_path / "config.json")
    store.save({"bulbs": bulbs()})
    store.save({"bulbs": bulbs(), "web_port": 8080})
    for p in (tmp_path / "config.json", tmp_path / "config.json.bak"):
        env = json.loads(p.read_text())
        if env["seq"] == 2:
            env["config"]["web_port"] = 9090                   # edited without a new checksum
            p.write_text(json.dumps(env))
    assert ConfigStore(tmp_path / "config.json").load()["web_port"] == 80


def test_store_all_damaged_raises(tmp_path):
    (tmp_path / "config.json").write_text("garbage")
    with pytest.raises(ConfigError, match="damaged"):
        ConfigStore(tmp_path / "config.json").load()


def test_save_rejects_invalid(tmp_path):
    store = ConfigStore(tmp_path / "config.json")
    with pytest.raises(ConfigError):
        store.save({"bulbs": {A: {"channel": 999}}})
    assert not (tmp_path / "config.json").exists()


def test_firmware_manifest_is_consistent():
    from engine import firmware
    imgs = firmware.load_manifest()
    assert imgs and all(len(i["sha256"]) == 64 and i["size"] > 0 and i["url"].startswith("http") for i in imgs)
    assert len({(i["model"], i["hw_ver"]) for i in imgs}) == len(imgs)   # one image per model/hw



def test_following_a_group_without_a_channel_is_flagged():
    cfg = validate({"groups": {"G": {"channel": None}},
                    "bulbs": {A: {"ip": "10.0.0.1", "follow": "G", "name": "a"}}})
    assert patch_conflicts(cfg) == ["a follows group G, which has no channel, so it gets no DMX"]
