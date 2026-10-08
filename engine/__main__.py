"""python -m engine COMMAND

    run                 run the engine (DMX in, bulbs out)
    import-django-db    copy the bulbs from the old Django app's db.sqlite3
    firmware fetch      download and verify the bulb firmware in firmware/manifest.json
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
    if args.web_port:
        cfg["web_port"] = args.web_port
    engine = Engine(store, cfg, receiver=receiver, discovery_targets=targets)

    async def serve():
        import uvicorn
        from engine.web import create_app
        app = create_app(engine, firmware_dir=args.firmware_dir)
        server = uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=cfg["web_port"],
                                               log_level="warning", lifespan="off"))
        eng = asyncio.create_task(engine.run())
        web = asyncio.create_task(server.serve())
        if args.status_file:
            asyncio.create_task(write_status(engine, args.status_file))
        logging.getLogger("engine").info("web UI on port %d", cfg["web_port"])
        done, _ = await asyncio.wait({eng, web}, return_when=asyncio.FIRST_COMPLETED)
        engine._stopping = True
        server.should_exit = True
        for t in done:
            t.result()          # surface a crash

    try:
        asyncio.run(serve())
    except KeyboardInterrupt:
        pass


async def write_status(engine, path, every=5.0):
    """Write the engine's status as JSON every few seconds (point it at RAM,
    e.g. /run/dmx_smartbulb/status.json, so the SD card stays quiet)."""
    import json
    import os
    await asyncio.sleep(2)
    while True:
        try:
            tmp = path + ".tmp"
            with open(tmp, "w") as f:
                json.dump({"time": __import__("time").time(), **engine.status()}, f, default=str)
            os.replace(tmp, path)
        except OSError as e:
            logging.getLogger("engine").warning("can't write status file: %s", e)
        await asyncio.sleep(every)


def cmd_import_django_db(args):
    """Old table: config_bulb(name, ip_addr, channel, enabled). Bulbs are keyed
    by MAC now. Bulbs are discovered (broadcast plus their old IPs) and matched
    to old rows by IP, or, when the IP has changed, by the last four hex digits
    of the MAC that the factory names end in ("TP-LINK_Smart Bulb_A86D")."""
    rows = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True).execute(
        "SELECT name, ip_addr, channel, enabled FROM config_bulb ORDER BY ip_addr").fetchall()
    store = ConfigStore(args.config)
    cfg = store.load()
    targets = ["255.255.255.255"] + [r[1] for r in rows]
    found = asyncio.run(kasa.discover(targets, port=cfg["kasa_port"], timeout=3.0))
    by_ip = {ip: info for ip, info in found.items()}
    by_suffix = {}
    for ip, info in found.items():
        mac = kasa.sysinfo_mac(info)
        if mac:
            by_suffix.setdefault(mac[-4:], []).append((ip, info))
    added, missing = 0, []
    for name, ip, channel, enabled in rows:
        hit = (ip, by_ip[ip]) if ip in by_ip else None
        suffix = name.strip()[-4:].upper()
        if hit is None and len(by_suffix.get(suffix, [])) == 1:
            hit = by_suffix[suffix][0]
        if hit is None:
            missing.append(f"{name} ({ip})")
            continue
        new_ip, info = hit
        mac = kasa.sysinfo_mac(info)
        cfg["bulbs"][mac] = {"name": name, "ip": new_ip,
                             "channel": channel if 1 <= channel <= 510 else None,
                             "dmx": bool(enabled)}
        moved = f" (now {new_ip})" if new_ip != ip else ""
        print(f"  {name}: {mac}, channel {channel}{moved}")
        added += 1
    try:
        store.save(cfg)
    except ConfigError as e:
        sys.exit("not saved: " + "; ".join(e.problems))
    print(f"imported {added} of {len(rows)} bulbs into {args.config}")
    if missing:
        print(f"{len(missing)} not found on the network (add them later from discovery):", ", ".join(missing))


def cmd_firmware(args):
    from engine import firmware
    if args.action == "fetch":
        ok, failed = firmware.fetch(args.dir)
        if failed:
            sys.exit(f"{len(failed)} image(s) failed")
    else:
        imgs = firmware.cached(args.dir)
        for img in firmware.load_manifest():
            mark = "cached" if img in imgs else "missing"
            print(f"{img['model']} hw {img['hw_ver']}  {img['version']}  {mark}")


def main():
    if sys.argv[1:2] == ["setup"]:
        from engine.setup import main as setup_main   # has its own options
        sys.exit(setup_main(sys.argv[2:]))
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
    run.add_argument("--web-port", type=int, help="override the configured web port (e.g. 8080 when developing)")
    run.add_argument("--firmware-dir", default="/var/lib/dmx_smartbulb/firmware")
    run.add_argument("--status-file", metavar="PATH", help="write status JSON here every 5 s")
    run.set_defaults(func=cmd_run)

    imp = sub.add_parser("import-django-db", help="import bulbs from the old app")
    imp.add_argument("db", nargs="?", default="/dmx_smartbulb/db.sqlite3")
    imp.set_defaults(func=cmd_import_django_db)

    fw = sub.add_parser("firmware", help="bulb firmware kept on the Pi")
    fw.add_argument("action", choices=("fetch", "list"))
    fw.add_argument("--dir", default="/var/lib/dmx_smartbulb/firmware", help="where to keep the images")
    fw.set_defaults(func=cmd_firmware)

    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    args.func(args)


if __name__ == "__main__":
    main()
