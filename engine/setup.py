"""`python -m engine setup`: configure a Raspberry Pi 4 to run DMX Smart Bulbs.

Run through ./setup.sh (which installs the apt packages and the venv first). Every
step looks at the current state before acting, so it is safe to re-run. Without
--yes it prints the plan and asks before changing anything; --check only reports.

Steps:
  preflight       Pi 4, Raspberry Pi OS bookworm 64-bit, root
  config.txt      UART3 on GPIO4/5 (pin 29), Bluetooth off, WiFi radio on/off
  services        ModemManager (probes serial ports), bluetooth, hciuart, triggerhappy off
  user            the `dmxbulb` service user, in the dialout group
  data            /var/lib/dmx_smartbulb (config + firmware), optionally importing a config
  quiet root      logs in RAM, no swap file on the SD card (zram stays), /tmp in RAM
  service         dmx_smartbulb.service, enabled and (re)started
  legacy          (--remove-old) the old runit service, nginx site and gunicorn logs
  firmware        download the bulb firmware images listed in firmware/manifest.json
After a reboot, --check also confirms the web UI answers, DMX is arriving and the SD
card stays quiet for a minute.
"""

import argparse
import datetime
import grp
import json
import os
import pwd
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
VENV = REPO / ".venv"
USER = "dmxbulb"
DATA = Path("/var/lib/dmx_smartbulb")
CONFIG_TXT = Path("/boot/firmware/config.txt")
UNIT = Path("/etc/systemd/system/dmx_smartbulb.service")
JOURNALD = Path("/etc/systemd/journald.conf.d/dmx_smartbulb.conf")
TMP_MOUNT = Path("/etc/systemd/system/tmp.mount")
BEGIN, END = "# BEGIN dmx_smartbulb", "# END dmx_smartbulb"
QUIET_SERVICES = ("ModemManager", "bluetooth", "hciuart", "triggerhappy")
# Things earlier versions of this project installed; removed with --remove-old.
LEGACY_RUNIT = (Path("/etc/service/dmx_smartbulb"), Path("/etc/sv/dmx_smartbulb"))
LEGACY_NGINX = (Path("/etc/nginx/sites-enabled/dmx_smartbulb"), Path("/etc/nginx/sites-available/dmx_smartbulb"))
LEGACY_UNITS = ("dmx-engine-dev",)
LEGACY_LOGS = Path("/var/log/gunicorn")


def sh(*cmd, check=True):
    return subprocess.run(cmd, check=check, capture_output=True, text=True)


def ok(cmd):
    return subprocess.run(cmd, capture_output=True).returncode == 0


class Step:
    """One idempotent change: `todo` is a list of human-readable actions still needed
    (empty when the system already matches); apply() makes them true."""

    def __init__(self, name, todo, apply=None, reboot=False):
        self.name, self.todo, self.apply, self.reboot = name, todo, apply, reboot


# ---- preflight -------------------------------------------------------------------


def preflight():
    problems = []
    model = Path("/proc/device-tree/model").read_text(errors="ignore").strip("\x00\n") \
        if Path("/proc/device-tree/model").exists() else "unknown"
    if "Raspberry Pi 4" not in model:
        problems.append(f"this is a {model!r}; only the Pi 4 is supported "
                        "(UART3 on GPIO4/5 doesn't exist on a Pi 3, and a Pi 5's UARTs differ)")
    osr = dict(re.findall(r'^(\w+)="?([^"\n]*)"?', Path("/etc/os-release").read_text(), re.M))
    if osr.get("VERSION_CODENAME") != "bookworm":
        problems.append(f"OS is {osr.get('PRETTY_NAME')}; Raspberry Pi OS bookworm is expected")
    if os.uname().machine != "aarch64":
        problems.append(f"64-bit OS expected, this is {os.uname().machine}")
    if os.geteuid() != 0:
        problems.append("run as root (sudo ./setup.sh)")
    return model, problems


# ---- config.txt ------------------------------------------------------------------


def config_txt_lines(wifi):
    lines = ["enable_uart=1", "dtoverlay=uart3", "dtoverlay=disable-bt"]
    if not wifi:
        lines.append("dtoverlay=disable-wifi")
    return lines


def step_config_txt(wifi):
    text = CONFIG_TXT.read_text()
    want = config_txt_lines(wifi)
    body = text
    if BEGIN in text:
        body = text[:text.index(BEGIN)] + text[text.index(END) + len(END):]
    # Settings outside our block that we manage (or that contradict it) get commented
    # out, so the block is the one place they're set.
    managed = re.compile(r"^\s*(enable_uart=|dtoverlay=uart3\b|dtoverlay=disable-bt\b|"
                         r"dtoverlay=disable-wifi\b)")
    outside = [ln for ln in body.splitlines() if managed.match(ln)]
    block = "\n".join([BEGIN, "[all]", *want, END])
    current_block = text[text.index(BEGIN):text.index(END) + len(END)] if BEGIN in text else None
    todo = []
    if current_block != block:
        todo.append(f"set in a marked block: {', '.join(want)}")
    if outside:
        todo.append(f"comment out the same settings elsewhere: {', '.join(s.strip() for s in outside)}")

    def apply():
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        shutil.copy2(CONFIG_TXT, CONFIG_TXT.with_name(f"config.txt.bak-{stamp}"))
        kept = [f"# dmx_smartbulb: {ln}" if managed.match(ln) else ln for ln in body.splitlines()]
        out = "\n".join(kept).rstrip("\n") + "\n\n" + block + "\n"
        tmp = CONFIG_TXT.with_suffix(".tmp")
        tmp.write_text(out)
        os.replace(tmp, CONFIG_TXT)
    return Step("config.txt", todo, apply, reboot=True)


# ---- services, user, data ----------------------------------------------------------


def unit_exists(name):
    return sh("systemctl", "list-unit-files", f"{name}.service", "--no-legend", check=False).stdout.strip() != ""


def step_services():
    todo = [f"stop and disable {s}" for s in QUIET_SERVICES
            if unit_exists(s) and (ok(["systemctl", "is-enabled", "--quiet", s]) or
                                   ok(["systemctl", "is-active", "--quiet", s]))]

    def apply():
        for s in QUIET_SERVICES:
            if unit_exists(s):
                sh("systemctl", "disable", "--now", s, check=False)
    return Step("services", todo, apply)


def step_user():
    todo = []
    try:
        pw = pwd.getpwnam(USER)
        if USER not in grp.getgrnam("dialout").gr_mem and pw.pw_gid != grp.getgrnam("dialout").gr_gid:
            todo.append(f"add {USER} to the dialout group")
    except KeyError:
        todo.append(f"create system user {USER} (no login, in the dialout group)")

    def apply():
        try:
            pwd.getpwnam(USER)
        except KeyError:
            sh("useradd", "--system", "--user-group", "--no-create-home", "--home-dir", str(DATA),
               "--shell", "/usr/sbin/nologin", USER)
        sh("usermod", "-aG", "dialout", USER)
    return Step("user", todo, apply)


def step_data(import_config):
    todo = []
    cfg = DATA / "config.json"
    if not DATA.is_dir():
        todo.append(f"create {DATA} (owned by {USER})")
    elif _owner(DATA) != USER:
        todo.append(f"give {DATA} to {USER}")
    if import_config and not cfg.exists():
        todo.append(f"import the config from {import_config}")
    elif import_config:
        todo.append(f"(not importing {import_config}: {cfg} already exists)")

    def apply():
        (DATA / "firmware").mkdir(parents=True, exist_ok=True)
        if import_config and not cfg.exists():
            src = Path(import_config)
            for suffix in ("", ".bak"):           # both checksummed copies, if present
                s = src.with_name(src.name + suffix)
                if s.exists():
                    shutil.copy2(s, cfg.with_name(cfg.name + suffix))
        sh("chown", "-R", f"{USER}:{USER}", str(DATA))
        os.chmod(DATA, 0o750)
    return Step("data", [t for t in todo if not t.startswith("(")] and todo, apply)


def _owner(path):
    try:
        return pwd.getpwuid(path.stat().st_uid).pw_name
    except KeyError:
        return None


# ---- quiet root --------------------------------------------------------------------


def step_quiet_root():
    todo = []
    src = REPO / "deploy" / "journald.conf"
    if not JOURNALD.exists() or JOURNALD.read_text() != src.read_text():
        todo.append("keep logs in RAM (journald Storage=volatile, 16 MB)")
    if ok(["systemctl", "is-enabled", "--quiet", "dphys-swapfile"]) or Path("/var/swap").exists():
        todo.append("turn off the SD-card swap file (zram swap stays)")
    if not ok(["systemctl", "is-enabled", "--quiet", "tmp.mount"]):
        todo.append("mount /tmp in RAM (from the next boot)")

    def apply():
        JOURNALD.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, JOURNALD)
        sh("systemctl", "restart", "systemd-journald", check=False)
        if unit_exists("dphys-swapfile"):
            sh("dphys-swapfile", "swapoff", check=False)
            sh("systemctl", "disable", "--now", "dphys-swapfile", check=False)
        if Path("/var/swap").exists():
            sh("swapoff", "/var/swap", check=False)
            Path("/var/swap").unlink()
        if not TMP_MOUNT.exists() and Path("/usr/share/systemd/tmp.mount").exists():
            shutil.copy2("/usr/share/systemd/tmp.mount", TMP_MOUNT)
        sh("systemctl", "daemon-reload")
        sh("systemctl", "enable", "tmp.mount", check=False)
    return Step("quiet root", todo, apply, reboot=True)


# ---- the service ---------------------------------------------------------------------


def unit_text():
    tpl = (REPO / "deploy" / "dmx_smartbulb.service.in").read_text()
    return tpl.replace("@REPO@", str(REPO)).replace("@VENV@", str(VENV))


def step_service():
    todo = []
    if not UNIT.exists() or UNIT.read_text() != unit_text():
        todo.append(f"install {UNIT.name} (runs {REPO} as {USER})")
    if not ok(["systemctl", "is-enabled", "--quiet", "dmx_smartbulb"]):
        todo.append("enable it at boot")
    if todo or not ok(["systemctl", "is-active", "--quiet", "dmx_smartbulb"]):
        todo.append("(re)start it")
    # Another unit holding port 80 would stop it starting.
    for u in LEGACY_UNITS:
        if unit_exists(u) and ok(["systemctl", "is-active", "--quiet", u]):
            todo.insert(0, f"stop {u} (it holds port 80; --remove-old deletes it)")

    def apply():
        for u in LEGACY_UNITS:
            if unit_exists(u):
                sh("systemctl", "disable", "--now", u, check=False)
        UNIT.write_text(unit_text())
        sh("systemctl", "daemon-reload")
        sh("systemctl", "enable", "dmx_smartbulb")
        sh("systemctl", "restart", "dmx_smartbulb")
    return Step("service", todo, apply)


def step_legacy():
    todo = []
    if any(p.exists() or p.is_symlink() for p in LEGACY_RUNIT):
        todo.append("remove the old runit service (/etc/sv/dmx_smartbulb, /etc/service/dmx_smartbulb)")
    if any(p.exists() or p.is_symlink() for p in LEGACY_NGINX):
        todo.append("remove the old nginx site")
    if unit_exists("nginx") and ok(["systemctl", "is-enabled", "--quiet", "nginx"]):
        todo.append("disable nginx (port 80 is the engine's now)")
    for u in LEGACY_UNITS:
        if Path(f"/etc/systemd/system/{u}.service").exists():
            todo.append(f"delete the {u} unit")
    if LEGACY_LOGS.exists():
        todo.append(f"delete {LEGACY_LOGS}")

    def apply():
        svc = LEGACY_RUNIT[0]
        if svc.exists() or svc.is_symlink():
            sh("sv", "down", str(svc), check=False)
            svc.unlink() if svc.is_symlink() else shutil.rmtree(svc)
        if LEGACY_RUNIT[1].exists():
            shutil.rmtree(LEGACY_RUNIT[1])
        for p in LEGACY_NGINX:
            if p.exists() or p.is_symlink():
                p.unlink()
        if unit_exists("nginx"):
            sh("systemctl", "disable", "--now", "nginx", check=False)
        for u in LEGACY_UNITS:
            f = Path(f"/etc/systemd/system/{u}.service")
            if f.exists():
                sh("systemctl", "disable", "--now", u, check=False)
                f.unlink()
                d = Path(f"/etc/systemd/system/{u}.service.d")
                if d.exists():
                    shutil.rmtree(d)
        sh("systemctl", "daemon-reload")
        if LEGACY_LOGS.exists():
            shutil.rmtree(LEGACY_LOGS)
    return Step("legacy", todo, apply)


def step_firmware():
    from engine import firmware
    have = {i["file"] for i in firmware.cached(DATA / "firmware")} if (DATA / "firmware").exists() else set()
    missing = [i for i in firmware.load_manifest() if i["file"] not in have]
    todo = [f"download {len(missing)} bulb firmware image(s) (needs internet)"] if missing else []

    def apply():
        r = subprocess.run([str(VENV / "bin" / "python"), "-m", "engine", "firmware", "fetch",
                            "--dir", str(DATA / "firmware")], cwd=REPO, capture_output=True, text=True)
        print(("    " + r.stdout.strip().replace("\n", "\n    ")) if r.stdout.strip() else "", end="")
        if r.returncode:
            print(f"    firmware download failed (try again later): {r.stderr.strip().splitlines()[-1:]}")
        sh("chown", "-R", f"{USER}:{USER}", str(DATA / "firmware"))
    return Step("firmware", todo, apply)


# ---- checks after install -----------------------------------------------------------


def sd_writes(seconds):
    """Sectors written to the SD card over `seconds` (and the top writers, if any)."""
    def sectors():
        for line in Path("/proc/diskstats").read_text().splitlines():
            f = line.split()
            if f[2] == "mmcblk0":
                return int(f[9])
        return 0
    a = sectors()
    time.sleep(seconds)
    return sectors() - a


def health(write_seconds):
    rows = []
    rows.append(("UART3 (/dev/ttyAMA3)", Path("/dev/ttyAMA3").exists(), ""))
    rows.append(("service active", ok(["systemctl", "is-active", "--quiet", "dmx_smartbulb"]), ""))
    try:
        with urllib.request.urlopen("http://127.0.0.1/api/session", timeout=3) as r:
            title = json.load(r).get("title", "")
        rows.append(("web UI answers", True, title))
    except OSError as e:
        rows.append(("web UI answers", False, str(e)))
    try:
        st = json.loads(Path("/dev/shm/dmx_status.json").read_text())
        frames = st.get("input", {}).get("frames", 0)
        rows.append(("DMX arriving", bool(st.get("dmx_present")),
                     f"{frames} frames so far" if frames else "no DMX source connected?"))
        rows.append(("bulbs online", st.get("online", 0) > 0, f"{st.get('online', 0)}/{len(st.get('bulbs', {}))}"))
    except (OSError, ValueError):
        rows.append(("DMX arriving", False, "no status file yet"))
    if write_seconds:
        n = sd_writes(write_seconds)
        rows.append((f"SD card quiet for {write_seconds} s", n == 0,
                     f"{n * 512 // 1024} KB written" + ("" if n == 0 else
                     " (an SSH session, git or an editor counts too)")))
    return rows


# ---- main -----------------------------------------------------------------------------


def main(argv=None):
    ap = argparse.ArgumentParser(prog="sudo ./setup.sh", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true", help="report what would change and the system's health; change nothing")
    ap.add_argument("--yes", "-y", action="store_true", help="don't ask before applying")
    ap.add_argument("--wifi", choices=("on", "off"), help="WiFi radio (default: keep the current setting; "
                    "on is only needed for pairing bulbs)")
    ap.add_argument("--hostname", help="set the hostname (reachable as <name>.local)")
    ap.add_argument("--import-config", metavar="PATH", help="start from this config.json (only if none exists yet)")
    ap.add_argument("--remove-old", action="store_true", help="remove the old Django/runit/nginx install")
    ap.add_argument("--no-firmware", action="store_true", help="skip downloading bulb firmware")
    ap.add_argument("--write-check", type=int, default=60, metavar="S",
                    help="seconds to watch for SD card writes in --check (0 to skip)")
    args = ap.parse_args(argv)

    model, problems = preflight()
    print(f"{model} · {REPO}")
    if problems:
        for p in problems:
            print(f"  ✗ {p}")
        return 1

    wifi = args.wifi == "on" if args.wifi else "dtoverlay=disable-wifi" not in CONFIG_TXT.read_text()
    steps = [step_config_txt(wifi), step_services(), step_user(), step_data(args.import_config),
             step_quiet_root(), step_service()]
    if args.remove_old:
        steps.append(step_legacy())
    if not args.no_firmware:
        steps.append(step_firmware())
    if args.hostname and args.hostname != os.uname().nodename:
        steps.insert(0, Step("hostname", [f"rename this Pi to {args.hostname} ({args.hostname}.local)"],
                             lambda: sh("hostnamectl", "set-hostname", args.hostname), reboot=True))

    pending = [s for s in steps if s.todo]
    print("\nPlan:" if pending else "\nNothing to change.")
    for s in steps:
        mark = "→" if s.todo else "✓"
        print(f"  {mark} {s.name}" + ("" if s.todo else ": ok"))
        for t in s.todo:
            print(f"      {t}")

    if args.check:
        print("\nHealth:")
        for name, good, note in health(args.write_check):
            print(f"  {'✓' if good else '✗'} {name}" + (f"  ({note})" if note else ""))
        return 0

    if pending:
        if not args.yes and input("\nApply these changes? [y/N] ").strip().lower() not in ("y", "yes"):
            print("Nothing changed.")
            return 1
        for s in pending:
            print(f"  {s.name}…")
            s.apply()
        reboot = any(s.reboot for s in pending)
        print("\nDone." + (" Reboot to finish (sudo reboot), then run sudo ./setup.sh --check." if reboot else ""))
    host = (args.hostname or os.uname().nodename)
    print(f"Web UI: http://{host}.local/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
