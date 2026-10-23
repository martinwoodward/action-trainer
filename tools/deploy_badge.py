"""Deploy the Airtime badge app over USB serial.

    python tools/deploy_badge.py                      # copy the app to /state/apps/airtime
    python tools/deploy_badge.py --autostart          # ...and launch it on power-on/reset
    python tools/deploy_badge.py --no-autostart       # go back to booting into the menu
    python tools/deploy_badge.py --name martin --model dist/model.py --run

The port is found automatically (USB 2e8a:1101); pass --port if more than one
badge is plugged in. Close anything else using the serial port first.

Autostart patches /system/main.py on the badge (backing up the original to
/state/main.orig.py) so that a power-on, reset-button or wake boot launches the
app named in /state/autostart. The boot animation still plays, and leaving the
app (HOME) or `mpremote reset` (a watchdog reset) still shows the menu.
"""
import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "badge" / "apps" / "airtime"
REMOTE_APP = "/state/apps/airtime"
USB_ID = "2e8a:1101"
AUTOSTART_MARKER = "# Optional autostart: /state/autostart"
# /system is mounted read-only; remount the same flash region writable.
REMOUNT_SYSTEM = ("import vfs, rp2; vfs.umount('/system'); "
                  "vfs.mount(vfs.VfsFat(rp2.Flash(start=0, len=13631488 - 1048576)), '/system')")

AUTOSTART_BLOCK = AUTOSTART_MARKER + """ holds an app dir (e.g. /state/apps/airtime).
# Only used on power-on/reset/wake; exiting the app (watchdog reset) shows the menu.
autostart = None
if machine.reset_cause() != machine.WDT_RESET:
    try:
        with open("/state/autostart") as file:
            autostart = file.read().strip()
        os.stat(autostart + "/__init__.py")
    except OSError:
        autostart = None
    if not autostart:
        autostart = None

"""
MENU_LAUNCH = 'app_to_launch = launch("/system/apps/menu")'


def patch_main(source):
    """Add autostart to the badge's boot script; returns it unchanged if already patched."""
    if AUTOSTART_MARKER in source:
        return source
    anchor = "\ntry:\n    import sparkle"
    if "import machine\n" not in source or anchor not in source or MENU_LAUNCH not in source:
        raise ValueError("unrecognised /system/main.py; not patching it")
    source = source.replace("import machine\n", "import machine\nimport os\n", 1)
    source = source.replace(anchor, "\n" + AUTOSTART_BLOCK.rstrip("\n") + "\n" + anchor, 1)
    return source.replace(MENU_LAUNCH, 'app_to_launch = autostart or launch("/system/apps/menu")', 1)


def find_port():
    out = subprocess.run([sys.executable, "-m", "mpremote", "devs"],
                         capture_output=True, text=True, check=True).stdout
    ports = [line.split()[0] for line in out.splitlines() if USB_ID in line]
    if len(ports) != 1:
        sys.exit(f"found {len(ports)} badges ({USB_ID}); pass --port" if ports
                 else "no badge found; plug it in over USB-C (and close Thonny/Web Serial)")
    return ports[0]


class Badge:
    def __init__(self, port):
        self.port = port

    def run(self, *args, capture=False):
        cmd = [sys.executable, "-m", "mpremote", "connect", self.port, *args]
        if capture:
            return subprocess.run(cmd, capture_output=True, text=True, check=True).stdout
        subprocess.run(cmd, check=True)

    def read(self, path):
        """File contents, or None if it doesn't exist."""
        code = (f"try:\n with open({path!r}) as f: print(f.read(), end='')\n"
                "except OSError: print('\\x00MISSING', end='')")
        out = self.run("exec", code, capture=True).replace("\r\n", "\n")
        return None if out == "\x00MISSING" else out


def write_text(badge, remote, text, prefix=(), suffix=()):
    with tempfile.TemporaryDirectory() as temp:
        local = Path(temp) / Path(remote).name
        local.write_text(text, encoding="utf-8", newline="\n")
        badge.run(*prefix, "fs", "cp", str(local), ":" + remote, *suffix)


def deploy_app(badge):
    files = sorted(p for p in APP.iterdir() if p.suffix in (".py", ".png"))
    args = ["exec", f"import os\nfor d in ('/state/apps', {REMOTE_APP!r}):\n"
                    "    try: os.mkdir(d)\n    except OSError: pass"]
    for f in files:
        args += ["+", "fs", "cp", str(f), f":{REMOTE_APP}/{f.name}"]
    badge.run(*args)
    print(f"copied {len(files)} files to {REMOTE_APP}")


def set_autostart(badge, enabled):
    if not enabled:
        badge.run("exec", "import os\ntry: os.remove('/state/autostart')\nexcept OSError: pass")
        print("autostart off: the badge boots into the menu")
        return
    main = badge.read("/system/main.py")
    patched = patch_main(main)
    if patched != main:
        if badge.read("/state/main.orig.py") is None:
            write_text(badge, "/state/main.orig.py", main)
            print("backed up the stock boot script to /state/main.orig.py")
        # Remount, copy and unmount in one session so the write is flushed.
        write_text(badge, "/system/main.py", patched,
                   prefix=("exec", REMOUNT_SYSTEM, "+"),
                   suffix=("+", "exec", "import vfs; vfs.umount('/system')"))
        print("patched /system/main.py for autostart")
    badge.run("exec", f"with open('/state/autostart', 'w') as f: f.write({REMOTE_APP!r})")
    print(f"autostart on: {REMOTE_APP} launches after the boot animation")


def set_name(badge, name):
    settings = json.loads(badge.read("/state/airtime.json") or "{}")
    settings["name"] = name[:16]
    write_text(badge, "/state/airtime.json", json.dumps(settings))
    print(f"named the badge {name[:16]!r}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", help="serial port, e.g. COM13 or /dev/ttyACM0 (default: auto-detect)")
    auto = ap.add_mutually_exclusive_group()
    auto.add_argument("--autostart", action="store_true", help="launch Airtime on power-on/reset")
    auto.add_argument("--no-autostart", action="store_true", help="boot into the menu again")
    ap.add_argument("--name", help="BLE name the badge advertises (max 16 chars)")
    ap.add_argument("--model", type=Path, help="generated model.py to install (normally sent by the recorder over BLE)")
    ap.add_argument("--skip-app", action="store_true", help="don't copy the app files")
    ap.add_argument("--run", action="store_true", help="start the app now over serial (Ctrl+C to detach)")
    args = ap.parse_args()

    badge = Badge(args.port or find_port())
    print("badge on", badge.port)
    if not args.skip_app:
        deploy_app(badge)
    if args.model:
        badge.run("fs", "cp", str(args.model), ":/state/airtime_model.py")
        print("installed", args.model)
    if args.name:
        set_name(badge, args.name)
    if args.autostart or args.no_autostart:
        set_autostart(badge, args.autostart)
    if args.run:
        badge.run("exec", f"exec(open('{REMOTE_APP}/__init__.py').read())")
    else:
        print("Press the badge's reset button to restart into the new version.")


if __name__ == "__main__":
    main()
