"""Deploy the Airtime app and the kiosk boot script to a badge over USB serial.

    python tools/deploy_badge.py app      # app + dist/model.py -> /state/apps/airtime
    python tools/deploy_badge.py kiosk    # also install badge/main.py as /system/main.py
    python tools/deploy_badge.py restore  # put the stock /system/main.py back

`/system` is a read-only FAT over serial. `kiosk` and `restore` remount the
same flash region read-write for the duration of one mpremote session, then
reset the badge.
"""

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_SRC = ROOT / "badge" / "apps" / "airtime" / "__init__.py"
PROTOCOL = ROOT / "protocol.py"
MODEL = ROOT / "dist" / "model.py"
KIOSK_MAIN = ROOT / "badge" / "main.py"
STOCK_MAIN = ROOT / "badge" / "main.orig.py"
USB_ID = "2e8a:1101"

STATE_APP = "/state/apps/airtime"
SYSTEM_APP = "/system/apps/airtime"

# Mirrors _boot_fat on the badge: the FAT spans user flash minus the LittleFS
# /state partition.
REMOUNT_RW = (
    "import vfs, rp2\n"
    "vfs.umount('/system')\n"
    "vfs.mount(vfs.VfsFat(rp2.Flash(start=0, len=13631488 - 1048576)), '/system')\n"
)
UNMOUNT = "import vfs\nvfs.umount('/system')\n"


def makedirs(path):
    return (
        "import os\n"
        "p = ''\n"
        f"for part in {path!r}.strip('/').split('/'):\n"
        "    p += '/' + part\n"
        "    try:\n"
        "        os.mkdir(p)\n"
        "    except OSError:\n"
        "        pass\n"
    )


def removedir(path):
    return (
        "import os\n"
        "try:\n"
        f"    for name in os.listdir({path!r}):\n"
        f"        os.remove({path!r} + '/' + name)\n"
        f"    os.rmdir({path!r})\n"
        "except OSError:\n"
        "    pass\n"
    )


def find_port():
    out = subprocess.run(
        [sys.executable, "-m", "mpremote", "devs"], capture_output=True, text=True, check=True
    ).stdout
    for line in out.splitlines():
        if USB_ID in line:
            return line.split()[0]
    sys.exit(f"No badge ({USB_ID}) found. Plug it in over USB and close other serial users.")


def mpremote(port, *chain):
    cmd = [sys.executable, "-m", "mpremote", "connect", port]
    for i, step in enumerate(chain):
        if i:
            cmd.append("+")
        cmd.extend(step)
    subprocess.run(cmd, check=True)


def app_files(dest):
    if not MODEL.exists():
        sys.exit(f"{MODEL} not found; run train.py first.")
    return [
        ["exec", makedirs(dest)],
        ["fs", "cp", str(APP_SRC), f":{dest}/__init__.py"],
        ["fs", "cp", str(PROTOCOL), f":{dest}/protocol.py"],
        ["fs", "cp", str(MODEL), f":{dest}/model.py"],
    ]


def deploy_app(port, args):
    chain = app_files(STATE_APP)
    if args.no_reset:
        chain.append(["exec", "--no-follow", f"exec(open('{STATE_APP}/__init__.py').read())"])
    else:
        chain.append(["reset"])
    mpremote(port, *chain)


def deploy_kiosk(port, args):
    mpremote(
        port,
        *app_files(STATE_APP),
        ["exec", REMOUNT_RW],
        *app_files(SYSTEM_APP),
        ["fs", "cp", str(KIOSK_MAIN), ":/system/main.py"],
        ["exec", UNMOUNT],
        ["reset"],
    )


def restore(port, args):
    mpremote(
        port,
        ["exec", REMOUNT_RW],
        ["fs", "cp", str(STOCK_MAIN), ":/system/main.py"],
        ["exec", removedir(SYSTEM_APP)],
        ["exec", UNMOUNT],
        ["reset"],
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", help="serial port (default: auto-detect)")
    sub = parser.add_subparsers(dest="command", required=True)
    app = sub.add_parser("app", help="copy the app and model to /state/apps/airtime")
    app.add_argument("--no-reset", action="store_true", help="start the app in place instead of resetting")
    app.set_defaults(func=deploy_app)
    sub.add_parser("kiosk", help="install the app and the kiosk /system/main.py").set_defaults(func=deploy_kiosk)
    sub.add_parser("restore", help="restore the stock /system/main.py").set_defaults(func=restore)
    args = parser.parse_args()
    args.func(args.port or find_port(), args)


if __name__ == "__main__":
    main()
