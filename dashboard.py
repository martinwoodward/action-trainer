"""Live dashboard: time each badge has spent in each state since it was reset.

Listens to badge advertisements only (no pairing), so it follows any number
of badges. Each badge counts milliseconds in still / throw / drop since boot
and advertises the totals in its scan response. The page can also zero a
badge; that stores a baseline here, which a badge reboot clears.

Usage:  python dashboard.py [--port 8766] [--no-browser]
"""
import argparse
import asyncio
import json
import time
import webbrowser
from pathlib import Path

from aiohttp import WSMsgType, web
from bleak import BleakScanner

from protocol import COMPANY_ID, STATS_UUID, unpack_mfg, unpack_stats

ROOT = Path(__file__).resolve().parent
WEB_DIR = ROOT / "web"
STATE_FILE = ROOT / "dashboard_state.json"
STATES = ("still", "throw", "drop")
# Advertised state codes (protocol.STATE_*) folded into the model's three.
STATE_BY_CODE = {0: "still", 1: "still", 2: "throw", 3: "throw", 4: "drop", 5: "still"}
OFFLINE_S = 20.0  # macOS reports a badge only every ~0.5-7.5 s, more after a rescan
HISTORY_S = 3600  # air-time history kept for the graph
SAVE_EVERY_S = 10.0
# A long-running CoreBluetooth scan can stop reporting a badge that left range
# and came back, while a fresh scan sees it at once. So restart the scan soon
# after a badge goes quiet, and at least every RESCAN_MAX_S forever after.
RESCAN_SILENT_S = 8.0
RESCAN_FAST_S = 120.0  # quiet for longer: rely on the RESCAN_MAX_S restarts
RESCAN_MIN_S = 10.0
RESCAN_MAX_S = 60.0


class Dashboard:
    def __init__(self, state_file=STATE_FILE):
        self.state_file = state_file
        self.badges = {}  # badge_id -> record
        # badge_id -> {"boot": n, "ms": {state: ms}}; totals at the page reset
        self.baselines = {}
        # badge_id -> [[epoch_ms, air_ms], ...]: shown air time when an advert arrived
        self.history = {}
        self.cleared_ms = 0  # Reset all: adverts heard before this are not charted
        self.load()

    # --- persistence -------------------------------------------------------
    def load(self):
        if self.state_file and self.state_file.exists():
            try:
                saved = json.loads(self.state_file.read_text(encoding="utf-8"))
            except ValueError:
                return
            self.baselines = saved.get("baselines", {})
            self.badges = saved.get("badges", {})
            self.history = saved.get("history", {})
            self.cleared_ms = saved.get("cleared_ms", 0)
            self.trim()

    def save(self):
        if self.state_file:
            data = {"baselines": self.baselines, "badges": self.badges, "history": self.history,
                    "cleared_ms": self.cleared_ms}
            tmp = self.state_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
            tmp.replace(self.state_file)

    # --- adverts -----------------------------------------------------------
    def observe(self, address, name, rssi, mfg, stats, now=None):
        """Apply one advertisement. mfg is unpack_mfg() output, stats unpack_stats()."""
        now = time.time() if now is None else now
        badge_id = "%08x" % mfg["badge_id"]
        rec = self.badges.setdefault(badge_id, {"badge_id": badge_id, "totals": None, "boot": None})
        rec.update(address=address, rssi=rssi, seen=now, state=STATE_BY_CODE.get(mfg["state"], "still"))
        if name:
            rec["name"] = name
        rec.setdefault("name", "badge-" + badge_id[-4:])
        if rec["boot"] is not None and rec["boot"] != mfg["boot"]:
            rec["totals"] = None  # rebooted: totals from the old boot no longer apply
        rec["boot"] = mfg["boot"]
        # The scan response can lag the advert across a reboot; only trust
        # totals that belong to the current boot.
        if stats is not None and stats["boot"] == mfg["boot"]:
            rec["totals"] = {s: stats[s + "_ms"] for s in STATES}
        base = self.baselines.get(badge_id)
        if base is not None and base["boot"] != mfg["boot"]:
            del self.baselines[badge_id]  # the reboot already zeroed the badge
            self.save()

    def shown_totals(self, badge_id):
        rec = self.badges[badge_id]
        if rec["totals"] is None:
            return None
        base = self.baselines.get(badge_id)
        if base is None or base["boot"] != rec["boot"]:
            return dict(rec["totals"])
        return {s: max(0, rec["totals"][s] - base["ms"][s]) for s in STATES}

    def reset(self, badge_id):
        rec = self.badges.get(badge_id)
        if rec is None or rec["totals"] is None:
            return False
        self.baselines[badge_id] = {"boot": rec["boot"], "ms": dict(rec["totals"])}
        self.save()
        return True

    def reset_all(self, now=None):
        """Zero every badge and clear the chart history."""
        now = time.time() if now is None else now
        self.history = {}
        self.cleared_ms = int(now * 1000)
        for badge_id in list(self.badges):
            self.reset(badge_id)
        self.save()

    def sample(self, now=None):
        """Add a history point for each badge heard since its last point.

        Points are stamped with the time the advert arrived, so a badge that
        was out of range resumes the same line; the chart joins the last point
        before the gap to the first one after it.
        """
        now = time.time() if now is None else now
        for badge_id, rec in self.badges.items():
            totals = self.shown_totals(badge_id)
            if totals is None:
                continue
            h = self.history.setdefault(badge_id, [])
            point = [int(rec["seen"] * 1000), totals["throw"]]
            if point[0] < self.cleared_ms or (h and point[0] <= h[-1][0]):
                continue  # nothing new heard
            if h and point[0] - h[-1][0] > OFFLINE_S * 1000:
                point.append(1)  # first point after a gap; the chart dashes the jump
            # A flat run of heard points only needs its first and last point.
            if len(point) == 2 and len(h) >= 2 and len(h[-1]) == 2 and h[-1][1] == h[-2][1] == point[1]:
                h[-1] = point
            else:
                h.append(point)
        self.trim(now)

    def trim(self, now=None):
        """Drop history points older than HISTORY_S."""
        now = time.time() if now is None else now
        cutoff = (now - HISTORY_S) * 1000
        for h in self.history.values():
            while h and h[0][0] < cutoff:
                h.pop(0)

    def needs_rescan(self, now=None):
        """True when a badge went quiet lately; older silences rely on the periodic rescan."""
        now = time.time() if now is None else now
        return any(RESCAN_SILENT_S <= now - rec["seen"] <= RESCAN_FAST_S for rec in self.badges.values())

    def snapshot(self, now=None):
        now = time.time() if now is None else now
        badges = []
        for badge_id, rec in self.badges.items():
            badges.append(
                {
                    "badge_id": badge_id,
                    "name": rec["name"],
                    "state": rec["state"],
                    "boot": rec["boot"],
                    "rssi": rec["rssi"],
                    "age": round(now - rec["seen"], 1),
                    "seen": int(rec["seen"] * 1000),
                    "online": now - rec["seen"] < OFFLINE_S,
                    "totals": self.shown_totals(badge_id),
                    "reset": badge_id in self.baselines,
                }
            )
        badges.sort(key=lambda b: (b["name"], b["badge_id"]))
        return {"type": "status", "badges": badges}


def decode(adv):
    data = adv.manufacturer_data.get(COMPANY_ID)
    if not data:
        return None, None
    mfg = unpack_mfg(COMPANY_ID.to_bytes(2, "little") + data)
    return mfg, unpack_stats(adv.service_data.get(STATS_UUID))


async def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--state", type=Path, default=STATE_FILE, help="baselines, badges and history (default: %(default)s)")
    args = parser.parse_args()

    dash = Dashboard(args.state)
    sockets = set()

    def on_adv(dev, adv):
        mfg, stats = decode(adv)
        if mfg is not None:
            dash.observe(dev.address, adv.local_name, adv.rssi, mfg, stats)

    async def index(_request):
        html = (WEB_DIR / "dashboard.html").read_text(encoding="utf-8")
        return web.Response(text=html, content_type="text/html", headers={"Cache-Control": "no-store"})

    async def ws_handler(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        sockets.add(ws)
        await ws.send_json({"type": "history", "history": dash.history, "cleared_ms": dash.cleared_ms})
        await ws.send_json(dash.snapshot())
        try:
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                op = json.loads(msg.data)
                if op.get("op") == "reset":
                    dash.reset(op.get("badge_id"))
                elif op.get("op") == "reset_all":
                    dash.reset_all()
                    history = {"type": "history", "history": dash.history, "cleared_ms": dash.cleared_ms}
                    for other in list(sockets):
                        try:
                            await other.send_json(history)
                        except (ConnectionError, RuntimeError):
                            sockets.discard(other)
                await ws.send_json(dash.snapshot())
        finally:
            sockets.discard(ws)
        return ws

    async def pump():
        last_save = time.time()
        while True:
            await asyncio.sleep(0.5)
            dash.sample()
            if time.time() - last_save >= SAVE_EVERY_S:
                dash.save()
                last_save = time.time()
            snap = dash.snapshot()
            for ws in list(sockets):
                try:
                    await ws.send_json(snap)
                except (ConnectionError, RuntimeError):
                    sockets.discard(ws)

    async def scan():
        while True:
            scanner = BleakScanner(on_adv)
            try:
                await scanner.start()
            except Exception as exc:  # e.g. Bluetooth switched off; keep trying
                print(time.strftime("%H:%M:%S"), "scan failed to start:", exc, flush=True)
                await asyncio.sleep(5)
                continue
            started = time.time()
            try:
                while True:
                    await asyncio.sleep(1)
                    age = time.time() - started
                    if age >= RESCAN_MAX_S or (age >= RESCAN_MIN_S and dash.needs_rescan()):
                        if age < RESCAN_MAX_S:
                            print(time.strftime("%H:%M:%S"), "a badge went quiet; restarting the scan", flush=True)
                        break
            finally:
                try:
                    await scanner.stop()
                except Exception as exc:
                    print(time.strftime("%H:%M:%S"), "scan failed to stop:", exc, flush=True)

    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/ws", ws_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "localhost", args.port).start()
    url = "http://localhost:%d" % args.port
    print("Dashboard on", url)
    if not args.no_browser:
        webbrowser.open(url)
    try:
        await asyncio.gather(scan(), pump())
    finally:
        dash.save()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
