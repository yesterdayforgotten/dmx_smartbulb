#!/usr/bin/env python3
"""Write dmx_esp32_wifisink/src/secrets.h (git-ignored) from the engine config's
show-network settings, so the WiFi password never goes into the repo.

    python3 tools/wifisink_secrets.py [--config PATH]
"""

import argparse
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent


def c_string(s):
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="/boot/firmware/dmx_smartbulb/config.json")
    args = ap.parse_args()
    env = json.loads(Path(args.config).read_text())
    net = env.get("config", env)["network"]
    if not net.get("ssid"):
        raise SystemExit("no show network in the config (set it in the web UI's Setup tab)")
    out = HERE / "dmx_esp32_wifisink" / "src" / "secrets.h"
    out.write_text(f"#pragma once\n#define WIFI_SSID {c_string(net['ssid'])}\n"
                   f"#define WIFI_PASSWORD {c_string(net.get('password', ''))}\n")
    out.chmod(0o600)
    print(f"wrote {out.relative_to(HERE)} for network {net['ssid']!r}")


if __name__ == "__main__":
    main()
