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
    step = setup.step_config_txt()
    assert step.todo and step.reboot
    step.apply()
    out = f.read_text()
    assert "dtparam=audio=on" in out and "[cm4]\notg_mode=1" in out and "dtparam=uart3=on" in out
    assert "# dmx_smartbulb: dtoverlay=uart3" in out                    # moved into the block
    block = out[out.index(setup.BEGIN):]
    assert block.splitlines()[1:5] == ["[all]", "enable_uart=1", "dtoverlay=uart3", "dtoverlay=disable-bt"]
    # The radio is switched off with rfkill, so the WiFi check works without a reboot.
    assert "dtoverlay=disable-wifi" not in block and "# dmx_smartbulb: dtoverlay=disable-wifi" in out
    assert len(list(tmp_path.glob("config.txt.bak-*"))) == 1
    assert setup.step_config_txt().todo == []                            # second run: nothing to do


def test_old_disable_wifi_block_is_rewritten(tmp_path, monkeypatch):
    f = tmp_path / "config.txt"
    f.write_text("arm_64bit=1\n\n" + "\n".join([setup.BEGIN, "[all]", "enable_uart=1", "dtoverlay=uart3",
                                                "dtoverlay=disable-bt", "dtoverlay=disable-wifi", setup.END]) + "\n")
    monkeypatch.setattr(setup, "CONFIG_TXT", f)
    step = setup.step_config_txt()
    assert step.todo
    step.apply()
    assert "disable-wifi" not in f.read_text() and setup.step_config_txt().todo == []


def test_wifi_helper_unit_points_at_this_checkout():
    text = setup.unit_text("dmx_smartbulb-wifi@.service.in")
    assert "@REPO@" not in text and f"{setup.REPO}/engine/wifi_helper.py" in text


def test_unit_points_at_this_checkout():
    text = setup.unit_text()
    assert "@REPO@" not in text and str(setup.REPO) in text and "User=dmxbulb" in text
