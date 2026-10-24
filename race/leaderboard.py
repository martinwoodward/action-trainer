"""Live leaderboard: time each badge has spent in each state since it was reset.

Badges report over Wi-Fi: each sends a UDP status datagram
(protocol.pack_status) to UDP port protocol.UDP_PORT on every state change and
every 250 ms, with its lifetime milliseconds in still / throw / drop. Badges
save these across reboots, so they only ever grow. The leaderboard acks each
badge (so broadcasting badges learn where it is) and can rename, identify and
ping badges. Resetting a badge on the page records its latest totals as an
offset; shown values are the lifetime totals minus that offset. Acks carry the
air-time offset so the badge can show its since-reset air time too.

Usage:  python leaderboard.py [--port 8766] [--udp-port 47217] [--no-browser]
"""
import argparse
import asyncio
import json
import time
import webbrowser
from pathlib import Path

from aiohttp import WSMsgType, web

import protocol

ROOT = Path(__file__).resolve().parent
WEB_DIR = ROOT / "web"
STATE_FILE = ROOT / "leaderboard_state.json"
STATES = ("still", "throw", "drop")
# Advertised state codes (protocol.STATE_*) folded into the model's three.
STATE_BY_CODE = {0: "still", 1: "still", 2: "throw", 3: "throw", 4: "drop", 5: "still"}
OFFLINE_S = 5.0  # badges send a heartbeat every 250 ms
HISTORY_S = 3600  # air-time history kept for the graph
SAVE_EVERY_S = 10.0
PUSH_MIN_S = 0.05  # reports heard within this are pushed to pages together
TICK_S = 0.5  # pages also get a status this often, to age offline badges
ACK_EVERY_S = 2.0  # how often each badge is told where the leaderboard is


class Leaderboard:
    def __init__(self, state_file=STATE_FILE):
        self.state_file = state_file
        self.badges = {}  # badge_id -> record
        # badge_id -> {"ms": {state: ms}}: lifetime totals at the page reset
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
            # Baselines with a "boot" are from per-boot badge totals; they don't match lifetime ones.
            self.baselines = {k: v for k, v in saved.get("baselines", {}).items() if "boot" not in v}
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
        rec.update(address=address, rssi=rssi, seen=now, state=STATE_BY_CODE.get(mfg["state"], "still"),
                   drops=mfg.get("drops"))
        if name:
            rec["name"] = name
        rec.setdefault("name", "badge-" + badge_id[-4:])
        rec["boot"] = mfg["boot"]
        if stats is not None and stats["boot"] == mfg["boot"]:
            rec["totals"] = {s: stats[s + "_ms"] for s in STATES}

    def observe_status(self, address, status, now=None):
        """Apply one UDP status datagram (protocol.unpack_status output)."""
        mfg = {"badge_id": status["badge_id"], "boot": status["boot"], "state": status["state"],
               "drops": status["drops"]}
        stats = {k: status[k] for k in ("boot", "still_ms", "throw_ms", "drop_ms")}
        self.observe(address, status["name"] or None, status["rssi"], mfg, stats, now)

    def shown_totals(self, badge_id):
        rec = self.badges[badge_id]
        if rec["totals"] is None:
            return None
        base = self.baselines.get(badge_id)
        if base is None:
            return dict(rec["totals"])
        # Clamped: a power cut can roll a badge back by up to a minute of still time.
        return {s: max(0, rec["totals"][s] - base["ms"][s]) for s in STATES}

    def offset_ms(self, badge_id):
        """Throw ms at the badge's last reset, sent to it in each ack."""
        base = self.baselines.get(badge_id)
        return base["ms"]["throw"] if base else 0

    def reset(self, badge_id):
        rec = self.badges.get(badge_id)
        if rec is None or rec["totals"] is None:
            return False
        self.baselines[badge_id] = {"ms": dict(rec["totals"])}
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
                    "drops": rec.get("drops"),  # lifetime count; the page plays UGF when it changes
                    "rssi": rec["rssi"],
                    "age": round(now - rec["seen"], 1),
                    "seen": int(rec["seen"] * 1000),
                    "online": now - rec["seen"] < OFFLINE_S,
                    "totals": self.shown_totals(badge_id),
                    "lifetime": rec["totals"],
                    "reset": badge_id in self.baselines,
                }
            )
        badges.sort(key=lambda b: (b["name"], b["badge_id"]))
        return {"type": "status", "badges": badges}


class BadgeLink(asyncio.DatagramProtocol):
    """UDP endpoint for badge reports; acks them and sends commands."""

    def __init__(self, dash, heard, on_reply=None):
        self.dash = dash
        self.heard = heard
        self.on_reply = on_reply
        self.transport = None
        self.addrs = {}  # badge_id -> (ip, port) last heard from
        self.acked = {}  # badge_id -> time of the last ack

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        msg = protocol.unpack_payload(data)
        if msg is None:
            return
        kind, raw_id, payload = msg
        badge_id = "%08x" % raw_id
        now = time.time()
        if kind == protocol.PKT_STATUS:
            status = protocol.unpack_status(data)
            if status is None:
                return
            self.addrs[badge_id] = addr
            self.dash.observe_status(addr[0], status, now)
            self.heard.set()
            if now - self.acked.get(badge_id, 0) >= ACK_EVERY_S:
                self.ack(badge_id)
        elif kind == protocol.PKT_PING:
            self.addrs[badge_id] = addr
            self.ack(badge_id)
        elif kind == protocol.PKT_REPLY and self.on_reply:
            self.on_reply(badge_id, payload)

    def ack(self, badge_id):
        """Ack a badge now, with its reset offset; False if never heard."""
        addr = self.addrs.get(badge_id)
        if addr is None or self.transport is None:
            return False
        self.acked[badge_id] = time.time()
        self.transport.sendto(protocol.pack_ack(int(badge_id, 16), self.dash.offset_ms(badge_id)), addr)
        return True

    def command(self, badge_id, op, arg=b""):
        """Send a command to a badge at its last address; False if never heard."""
        addr = self.addrs.get(badge_id)
        if addr is None or self.transport is None:
            return False
        self.transport.sendto(protocol.pack_cmd(int(badge_id, 16), op, arg), addr)
        return True


async def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--udp-port", type=int, default=protocol.UDP_PORT, help="badge reports (default: %(default)s)")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--state", type=Path, default=STATE_FILE, help="baselines, badges and history (default: %(default)s)")
    args = parser.parse_args()

    dash = Leaderboard(args.state)
    sockets = set()
    heard = asyncio.Event()
    link = BadgeLink(dash, heard)

    async def index(_request):
        html = (WEB_DIR / "leaderboard.html").read_text(encoding="utf-8")
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
                    link.ack(op.get("badge_id", ""))  # the badge screen zeroes now, not at the next ack
                elif op.get("op") == "rename":
                    name = str(op.get("name", "")).strip()[: protocol.UDP_NAME_MAX]
                    if name:
                        link.command(op.get("badge_id", ""), protocol.CMD_NAME, name.encode())
                elif op.get("op") == "identify":
                    link.command(op.get("badge_id", ""), protocol.CMD_IDENTIFY)
                elif op.get("op") == "reset_all":
                    dash.reset_all()
                    for badge_id in list(dash.badges):
                        link.ack(badge_id)
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
            # Push as soon as an advert arrives, so pages lag the badge as little
            # as possible; otherwise every TICK_S.
            try:
                await asyncio.wait_for(heard.wait(), TICK_S)
                await asyncio.sleep(PUSH_MIN_S)
            except asyncio.TimeoutError:
                pass
            heard.clear()
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

    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/ws", ws_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "localhost", args.port).start()
    loop = asyncio.get_running_loop()
    await loop.create_datagram_endpoint(lambda: link, local_addr=("0.0.0.0", args.udp_port))
    url = "http://localhost:%d" % args.port
    print("Leaderboard on", url, "| badges report to UDP port", args.udp_port, flush=True)
    if not args.no_browser:
        webbrowser.open(url)
    try:
        await pump()
    finally:
        dash.save()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
