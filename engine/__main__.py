"""python -m engine COMMAND

    run                 run the engine (DMX in, bulbs out)
    import-django-db    copy the bulbs from the old Django app's db.sqlite3
"""

import argparse
import asyncio
import logging
import sqlite3
import sys

from engine import kasa
from engine.config import DEFAULT_PATH, ConfigError, ConfigStore
from engine.core import Engine
from engine.receiver import Receiver


def cmd_run(args):
    store = ConfigStore(args.config)
    cfg = store.load()
    if args.replay:
        receiver = Receiver("replay", args.replay)
    elif args.input:
        receiver = Receiver(args.input, args.port or cfg["input"]["port"], rt_priority=50)
    else:
        receiver = None
    targets = args.discover.split(",") if args.discover else None
    engine = Engine(store, cfg, receiver=receiver, discovery_targets=targets)
    try:
        asyncio.run(engine.run())
    except KeyboardInterrupt:
        pass


def cmd_import_django_db(args):
    """Old table: config_bulb(name, ip_addr, channel, enabled). Bulbs are keyed
    by MAC now, so each one is asked for its sysinfo; bulbs that don't answer
    are listed and left out."""
    rows = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True).execute(
        "SELECT name, ip_addr, channel, enabled FROM config_bulb ORDER BY ip_addr").fetchall()
    store = ConfigStore(args.config)
    cfg = store.load()
    found = asyncio.run(kasa.discover([r[1] for r in rows], port=cfg["kasa_port"], timeout=2.0))
    added, missing = 0, []
    for name, ip, channel, enabled in rows:
        info = found.get(ip)
        mac = kasa.sysinfo_mac(info) if info else None
        if not mac:
            missing.append(f"{name} ({ip})")
            continue
        cfg["bulbs"][mac] = {"name": name, "ip": ip,
                             "channel": channel if 1 <= channel <= 510 else None,
                             "dmx": bool(enabled)}
        added += 1
    try:
        store.save(cfg)
    except ConfigError as e:
        sys.exit("not saved: " + "; ".join(e.problems))
    print(f"imported {added} of {len(rows)} bulbs into {args.config}")
    if missing:
        print("not answering (add them later from discovery):", ", ".join(missing))


def main():
    ap = argparse.ArgumentParser(prog="python -m engine", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=str(DEFAULT_PATH))
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="run the engine")
    run.add_argument("--input", choices=("uart", "esp32"), help="override the configured input")
    run.add_argument("--port", help="override the configured serial port")
    run.add_argument("--replay", metavar="FILE", help="play a dmx_rx_probe recording instead of reading DMX")
    run.add_argument("--discover", metavar="IPS", help="comma-separated discovery targets (default: broadcast)")
    run.set_defaults(func=cmd_run)

    imp = sub.add_parser("import-django-db", help="import bulbs from the old app")
    imp.add_argument("db", nargs="?", default="/dmx_smartbulb/db.sqlite3")
    imp.set_defaults(func=cmd_import_django_db)

    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    args.func(args)


if __name__ == "__main__":
    main()
