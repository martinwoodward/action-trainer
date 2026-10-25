"""Deploy the Race app and the kiosk boot script to a badge over USB serial.

    python tools/deploy_race.py app      # app + dist/race_model.py (if built) -> /state/apps/race
    python tools/deploy_race.py kiosk    # also install badge/main.py as /system/main.py
    python tools/deploy_race.py restore  # put the stock /system/main.py back

Wi-Fi: `app` and `kiosk` take --wifi-ssid/--wifi-password (or the
AIRTIME_WIFI_SSID/AIRTIME_WIFI_PASSWORD environment variables) and --server
(an IP, `auto` for this computer's address, or `none` to broadcast). They are
merged into /state/airtime_net.json on the badge; settings not given are kept.
Without an SSID there, the badge falls back to WIFI_SSID in /system/secrets.py.
The password is never printed or written into this repository.

`/system` is a read-only FAT over serial. `kiosk` and `restore` remount the
same flash region read-write for the duration of one mpremote session, then
reset the badge.
"""

import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_SRC = ROOT / "badge" / "apps" / "race" / "__init__.py"
APP_ASSETS = ROOT / "badge" / "apps" / "race" / "assets"
PROTOCOL = ROOT / "protocol.py"
MODEL = ROOT / "dist" / "race_model.py"
KIOSK_MAIN = ROOT / "badge" / "main.py"
STOCK_MAIN = ROOT / "badge" / "main.orig.py"
USB_ID = "2e8a:1101"

NET_CONFIG = "/state/airtime_net.json"
NET_UPLOAD = "/state/airtime_net.new"
STATE_APP = "/state/apps/race"
SYSTEM_APP = "/system/apps/race"
# Where the app lived under its earlier names; kiosk and restore delete these.
OLD_APPS = ("/state/apps/airtime", "/system/apps/airtime", "/state/apps/leaderboard", "/system/apps/leaderboard")

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


def removedirs(*paths):
    return (
        "import os\n"
        "def rm(p):\n"
        "    try:\n"
        "        for name in os.listdir(p):\n"
        "            rm(p + '/' + name)\n"
        "        os.rmdir(p)\n"
        "    except OSError:\n"
        "        try:\n"
        "            os.remove(p)\n"
        "        except OSError:\n"
        "            pass\n"
        f"for p in {paths!r}:\n"
        "    rm(p)\n"
    )


# Merges the uploaded settings into the badge's config; "" removes a key.
MERGE_NET = (
    "import json, os\n"
    "cfg = {}\n"
    "try:\n"
    f"    cfg = json.load(open({NET_CONFIG!r}))\n"
    "except (OSError, ValueError):\n"
    "    pass\n"
    f"for k, v in json.load(open({NET_UPLOAD!r})).items():\n"
    "    if v == '':\n"
    "        cfg.pop(k, None)\n"
    "    else:\n"
    "        cfg[k] = v\n"
    f"with open({NET_CONFIG!r}, 'w') as f:\n"
    "    json.dump(cfg, f)\n"
    f"os.remove({NET_UPLOAD!r})\n"
    "print('wifi config:', 'ssid set' if cfg.get('ssid') else 'no ssid', '| server', cfg.get('server') or 'broadcast')\n"
)


def local_ip():
    """This computer's address on the default route (no packets are sent)."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect(("192.0.2.1", 9))
        return s.getsockname()[0]


def net_settings(args):
    out = {}
    ssid = args.wifi_ssid or os.environ.get("AIRTIME_WIFI_SSID")
    password = args.wifi_password or os.environ.get("AIRTIME_WIFI_PASSWORD")
    if ssid:
        out["ssid"] = ssid
    if password is not None:
        out["password"] = password
    if args.server:
        out["server"] = "" if args.server == "none" else local_ip() if args.server == "auto" else args.server
    return out


def net_steps(args, tmpdir):
    settings = net_settings(args)
    if not settings:
        return []
    path = Path(tmpdir) / "airtime_net.json"
    path.write_text(json.dumps(settings), encoding="utf-8")
    os.chmod(path, 0o600)
    return [["fs", "cp", str(path), f":{NET_UPLOAD}"], ["exec", MERGE_NET]]


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
    model = []
    if MODEL.exists():
        model = [["fs", "cp", str(MODEL), f":{dest}/model.py"]]
    else:
        print(f"{MODEL} not found; skipping it (the badge keeps any model it has, else uses its heuristic detector).")
    return [
        ["exec", makedirs(dest)],
        ["fs", "cp", str(APP_SRC), f":{dest}/__init__.py"],
        ["fs", "cp", str(PROTOCOL), f":{dest}/protocol.py"],
        *model,
        ["exec", makedirs(f"{dest}/assets")],
        *(["fs", "cp", str(png), f":{dest}/assets/{png.name}"] for png in sorted(APP_ASSETS.glob("*.png"))),
    ]


def deploy_app(port, args):
    with tempfile.TemporaryDirectory() as tmp:
        _deploy_app(port, args, net_steps(args, tmp))


def _deploy_app(port, args, net):
    chain = app_files(STATE_APP) + net
    if args.no_reset:
        chain.append(["exec", "--no-follow", f"exec(open('{STATE_APP}/__init__.py').read())"])
    else:
        chain.append(["reset"])
    mpremote(port, *chain)


def deploy_kiosk(port, args):
    with tempfile.TemporaryDirectory() as tmp:
        _deploy_kiosk(port, args, net_steps(args, tmp))


def _deploy_kiosk(port, args, net):
    mpremote(
        port,
        *app_files(STATE_APP),
        *net,
        ["exec", REMOUNT_RW],
        ["exec", removedirs(*OLD_APPS)],
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
        ["exec", removedirs(SYSTEM_APP, *OLD_APPS)],
        ["exec", UNMOUNT],
        ["reset"],
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", help="serial port (default: auto-detect)")
    sub = parser.add_subparsers(dest="command", required=True)
    app = sub.add_parser("app", help="copy the app and model to /state/apps/race")
    app.add_argument("--no-reset", action="store_true", help="start the app in place instead of resetting")
    app.set_defaults(func=deploy_app)
    kiosk = sub.add_parser("kiosk", help="install the app and the kiosk /system/main.py")
    kiosk.set_defaults(func=deploy_kiosk)
    for p in (app, kiosk):
        p.add_argument("--wifi-ssid", help="Wi-Fi network (default: $AIRTIME_WIFI_SSID, else keep)")
        p.add_argument("--wifi-password", help="Wi-Fi password (default: $AIRTIME_WIFI_PASSWORD, else keep)")
        p.add_argument("--server", help="leaderboard IP, 'auto' (this computer) or 'none' (broadcast)")
    sub.add_parser("restore", help="restore the stock /system/main.py").set_defaults(func=restore)
    args = parser.parse_args()
    args.func(args.port or find_port(), args)


if __name__ == "__main__":
    main()
