"""setup's config.txt editing: idempotent, keeps everything else, backs up."""

from engine import setup

CURRENT = """dtparam=audio=on
arm_64bit=1
[cm4]
otg_mode=1
[all]
enable_uart=1
dtoverlay=disable-bt
dtoverlay=disable-wifi
dtoverlay=uart3
dtparam=uart3=on
"""


def test_config_txt_block_is_idempotent(tmp_path, monkeypatch):
    f = tmp_path / "config.txt"
    f.write_text(CURRENT)
    monkeypatch.setattr(setup, "CONFIG_TXT", f)
    step = setup.step_config_txt(wifi=False)
    assert step.todo and step.reboot
    step.apply()
    out = f.read_text()
    assert "dtparam=audio=on" in out and "[cm4]\notg_mode=1" in out and "dtparam=uart3=on" in out
    assert "# dmx_smartbulb: dtoverlay=uart3" in out                    # moved into the block
    block = out[out.index(setup.BEGIN):]
    assert block.splitlines()[1:5] == ["[all]", "enable_uart=1", "dtoverlay=uart3", "dtoverlay=disable-bt"]
    assert "dtoverlay=disable-wifi" in block
    assert len(list(tmp_path.glob("config.txt.bak-*"))) == 1
    assert setup.step_config_txt(wifi=False).todo == []                  # second run: nothing to do
    # Turning the WiFi radio on rewrites only the block.
    step = setup.step_config_txt(wifi=True)
    assert step.todo
    step.apply()
    out = f.read_text()
    assert "dtoverlay=disable-wifi" not in out[out.index(setup.BEGIN):]
    assert out.count(setup.BEGIN) == 1 and setup.step_config_txt(wifi=True).todo == []


def test_unit_points_at_this_checkout():
    text = setup.unit_text()
    assert "@REPO@" not in text and str(setup.REPO) in text and "User=dmxbulb" in text
