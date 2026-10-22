"""Training-data recorder for badge motion models.

Runs a local web UI (http://localhost:8765) backed by bleak. Pick a badge,
connect over GATT, pick an action, and record a session; the badge streams
every IMU sample (with its detector state) and the session is saved to
data/<label>/<timestamp>_<badge>.csv plus a .json sidecar.

Usage:  python recorder.py [--port 8765] [--no-browser]
"""
import argparse
import asyncio
import csv
import json
import math
import random
import time
import webbrowser
from datetime import datetime
from pathlib import Path

from aiohttp import WSMsgType, web
from bleak import BleakClient, BleakScanner

ROOT = Path(__file__).resolve().parent
from protocol import (
    CMD_IDENTIFY,
    CMD_LABEL,
    CMD_NAME,
    COMPANY_ID,
    INFO_UUID,
    STREAM_UUID,
    CONTROL_UUID,
    unpack_mfg,
    unpack_samples,
)
from features import FREEFALL_G

DATA_DIR = ROOT / "data"
WEB_DIR = ROOT / "web"
LABELS = ("throw", "drop", "still", "walk", "unknown")
# A guided session mixes still, held, thrown and dropped segments in one go.
MIXED = "mixed"
# A landings session only throws: each take is a catch (THROW) or a miss.
LANDINGS = "landings"
GUIDED = (MIXED, LANDINGS)
SESSION_TYPES = LABELS + GUIDED
# A drop is a throw that is not caught, so the drop prompt asks for a miss.
PROMPTS = {"still": "PUT DOWN", "hold": "HOLD", "throw": "THROW", "drop": "MISS"}
STILL_MS = 4000
HOLD_MS = 3000
FLIGHT_MIN_MS = 60  # free fall this long counts as a flight
SETTLE_MS = 1500  # quiet time after the last free fall before moving on
FLIGHT_TIMEOUT_MS = 20000
STATE_NAMES = ("STILL", "WALK", "FLYING", "THROW", "DROP", "HANDLING")
FLYING, THROW, DROP = 2, 3, 4
# The heuristic detector reports FLYING; a model-driven badge reports THROW/DROP.
AIRBORNE_STATES = (FLYING, THROW, DROP)
LSB_PER_G = 16384.0
CSV_HEADER = ("t_ms", "ax", "ay", "az", "gx", "gy", "gz", "events", "state")


def guided_script(flights_each, rng=random):
    """Shuffled (label, duration_ms) steps; flight steps have no duration."""
    kinds = ["throw"] * flights_each + ["drop"] * flights_each
    rng.shuffle(kinds)
    steps = [("still", STILL_MS)]
    for kind in kinds:
        steps.append(("hold", HOLD_MS))
        steps.append((kind, None))
        # Drops land on the floor; some throws are followed by setting it down.
        if kind == "drop" or rng.random() < 0.3:
            steps.append(("still", STILL_MS))
    return steps


def landings_script(flights_each, rng=random):
    """Shuffled catches and misses only. Each prompt waits for the throw and
    moves on once the landing has settled, so there is time to pick it up."""
    kinds = ["throw"] * flights_each + ["drop"] * flights_each
    rng.shuffle(kinds)
    return [(kind, None) for kind in kinds]


SCRIPTS = {MIXED: guided_script, LANDINGS: landings_script}


def session_counts():
    counts = {}
    for label in SESSION_TYPES:
        folder = DATA_DIR / label
        counts[label] = len(list(folder.glob("*.csv"))) if folder.exists() else 0
    return counts


class Recorder:
    def __init__(self):
        self.badges = {}  # address -> dict
        self.devices = {}  # address -> BLEDevice
        self.sockets = set()
        self.client = None
        self.address = None
        self.info = ""
        self.connecting = False
        self.reconnect_to = None  # badge to reconnect to after an unexpected drop
        self.disconnected_at = 0.0
        self.next_reconnect = 0.0
        self.sent_prompt = None
        self.last_seq = None
        self.lost = 0
        self.t_last16 = None
        self.t_ms = 0
        self.recording = None  # dict while recording
        self.pending = None  # finished recording awaiting save/discard
        self.live = []  # samples to push to the UI

    # --- scanning ----------------------------------------------------------
    def on_adv(self, dev, adv):
        mfg = adv.manufacturer_data.get(COMPANY_ID)
        if not mfg:
            return
        d = unpack_mfg(COMPANY_ID.to_bytes(2, "little") + mfg)
        if d is None:
            return
        self.devices[dev.address] = dev
        b = self.badges.setdefault(dev.address, {"address": dev.address})
        b.update(
            name=adv.local_name or b.get("name") or "?",
            badge_id="%08x" % d["badge_id"],
            rssi=adv.rssi,
            state=STATE_NAMES[d["state"]] if d["state"] < len(STATE_NAMES) else "?",
            seen=time.time(),
        )

    # --- GATT --------------------------------------------------------------
    async def connect(self, address):
        if self.client is not None or self.connecting:
            return
        dev = self.devices.get(address)
        if dev is None:
            raise RuntimeError("badge not seen yet")
        self.connecting = True
        await self.broadcast_status()
        try:
            client = BleakClient(dev, timeout=20, disconnected_callback=self.on_disconnect)
            await client.connect()
            self.client = client
            self.address = address
            self.reconnect_to = address
            self.sent_prompt = None
            self.last_seq = None
            self.lost = 0
            self.t_last16 = None
            self.info = (await client.read_gatt_char(INFO_UUID)).decode(errors="replace")
            await client.start_notify(STREAM_UUID, self.on_stream)
        finally:
            self.connecting = False
        await self.broadcast_status()

    def on_disconnect(self, _client):
        self.loop.call_soon_threadsafe(self._handle_disconnect)

    def _handle_disconnect(self):
        self.client = None
        self.disconnected_at = time.time()
        if self.recording is not None:
            self.finish_recording(reason="disconnected")
        self.loop.create_task(self.broadcast_status())

    def should_reconnect(self, now):
        """Reconnect once the badge advertises again after an unexpected drop
        (for example the reset switch rebooting a kiosk badge)."""
        if self.client is not None or self.connecting or self.reconnect_to is None:
            return False
        if now < self.next_reconnect:
            return False
        seen = self.badges.get(self.reconnect_to, {}).get("seen", 0)
        return seen > self.disconnected_at

    async def auto_reconnect(self):
        self.next_reconnect = time.time() + 3
        try:
            await self.connect(self.reconnect_to)
        except Exception as error:  # noqa: BLE001
            print("reconnect failed:", error)

    async def disconnect(self):
        self.reconnect_to = None
        client = self.client
        if client is not None:
            try:
                await self.command(CMD_LABEL, "")
            except Exception:
                pass
            await client.disconnect()
        self.client = None
        await self.broadcast_status()

    async def command(self, op, arg=""):
        if self.client is None:
            raise RuntimeError("not connected")
        await self.client.write_gatt_char(CONTROL_UUID, op + arg.encode()[:60], response=True)

    def on_stream(self, _char, data):
        seq, samples = unpack_samples(bytes(data))
        if self.last_seq is not None:
            self.lost += (seq - self.last_seq - 1) & 0xFFFF
        self.last_seq = seq
        for t16, ax, ay, az, gx, gy, gz, events, state in samples:
            if self.t_last16 is not None:
                self.t_ms += (t16 - self.t_last16) & 0xFFFF
            self.t_last16 = t16
            row = (self.t_ms, ax, ay, az, gx, gy, gz, events, state)
            rec = self.recording
            mag = math.sqrt(ax * ax + ay * ay + az * az) / LSB_PER_G
            if rec is not None:
                rec["rows"].append(row)
                if rec["script"] is not None:
                    self.advance_script(rec, self.t_ms, mag)
                elif state in AIRBORNE_STATES and rec["prev_state"] not in AIRBORNE_STATES:
                    rec["flights"] += 1
                rec["prev_state"] = state
            self.live.append((self.t_ms, round(mag, 3), state))

    # --- recording ---------------------------------------------------------
    def start_recording(self, label, takes, plushy, notes):
        if label not in SESSION_TYPES:
            raise RuntimeError("unknown label")
        self.pending = None
        self.recording = {
            "script": SCRIPTS[label](takes) if label in SCRIPTS else None,
            "step": 0,
            "step_start": None,
            "air_since": None,
            "last_air": None,
            "flight_at": None,
            "segments": [],
            "missed": 0,
            "label": label,
            "takes": takes,
            "plushy": plushy,
            "notes": notes,
            "rows": [],
            "marks": [],
            "flights": 0,
            "prev_state": None,
            "started": datetime.now(),
        }

    def advance_script(self, rec, t_ms, mag):
        if rec["step_start"] is None:
            rec["step_start"] = t_ms
        label, duration = rec["script"][rec["step"]]
        elapsed = t_ms - rec["step_start"]
        if duration is not None:
            done = elapsed >= duration
        else:
            if mag < FREEFALL_G:
                if rec["air_since"] is None:
                    rec["air_since"] = t_ms
                rec["last_air"] = t_ms
                if rec["flight_at"] is None and t_ms - rec["air_since"] >= FLIGHT_MIN_MS:
                    rec["flight_at"] = rec["air_since"]
                    rec["flights"] += 1
            else:
                rec["air_since"] = None
            if rec["flight_at"] is not None:
                done = t_ms - rec["last_air"] >= SETTLE_MS
            else:
                done = elapsed >= FLIGHT_TIMEOUT_MS
                rec["missed"] += done
        if not done:
            return
        rec["segments"].append(
            {"label": label, "start_ms": rec["step_start"], "end_ms": t_ms, "flight_ms": rec["flight_at"]}
        )
        rec["step"] += 1
        rec["step_start"] = t_ms
        rec["air_since"] = rec["last_air"] = rec["flight_at"] = None
        if rec["step"] >= len(rec["script"]):
            self.finish_recording(reason="script complete")

    def mark(self):
        rec = self.recording
        if rec is not None:
            rec["marks"].append(self.t_ms)

    def finish_recording(self, reason="stopped"):
        rec, self.recording = self.recording, None
        if rec is None:
            return
        rows = rec["rows"]
        duration = (rows[-1][0] - rows[0][0]) / 1000 if len(rows) > 1 else 0
        rec["duration_s"] = round(duration, 2)
        rec["rate_hz"] = round((len(rows) - 1) / duration, 1) if duration else 0
        rec["reason"] = reason
        rec["lost_packets"] = self.lost
        if rec["script"] is not None and rec["step"] < len(rec["script"]) and rec["step_start"] is not None and rows:
            label, _ = rec["script"][rec["step"]]
            rec["segments"].append(
                {"label": label, "start_ms": rec["step_start"], "end_ms": rows[-1][0], "flight_ms": rec["flight_at"]}
            )
        self.pending = rec

    def save_pending(self):
        rec, self.pending = self.pending, None
        if rec is None or not rec["rows"]:
            return None
        badge = self.badges.get(self.address, {})
        stamp = rec["started"].strftime("%Y%m%d-%H%M%S")
        folder = DATA_DIR / rec["label"]
        folder.mkdir(parents=True, exist_ok=True)
        stem = "%s_%s" % (stamp, badge.get("badge_id", "unknown"))
        t0 = rec["rows"][0][0]
        with open(folder / (stem + ".csv"), "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(CSV_HEADER)
            for row in rec["rows"]:
                w.writerow((row[0] - t0,) + row[1:])
        meta = {
            "label": rec["label"],
            "badge_id": badge.get("badge_id"),
            "badge_name": badge.get("name"),
            "badge_info": self.info,
            "started": rec["started"].isoformat(timespec="seconds"),
            "duration_s": rec["duration_s"],
            "samples": len(rec["rows"]),
            "rate_hz": rec["rate_hz"],
            "takes_target": rec["takes"],
            "flights_detected": rec["flights"],
            "marks_ms": [m - t0 for m in rec["marks"]],
            "plushy": rec["plushy"],
            "notes": rec["notes"],
            "lost_packets": rec["lost_packets"],
            "end_reason": rec["reason"],
            "lsb_per_g": LSB_PER_G,
        }
        if rec["script"] is not None:
            meta["segments"] = [
                {
                    "label": seg["label"],
                    "start_ms": seg["start_ms"] - t0,
                    "end_ms": seg["end_ms"] - t0,
                    "flight_ms": None if seg["flight_ms"] is None else seg["flight_ms"] - t0,
                }
                for seg in rec["segments"]
            ]
            meta["missed_flights"] = rec["missed"]
            meta["freefall_g"] = FREEFALL_G
        (folder / (stem + ".json")).write_text(json.dumps(meta, indent=2))
        return str((folder / (stem + ".csv")).relative_to(ROOT))

    def badge_prompt(self):
        rec = self.recording
        if rec is None:
            return ""
        label = rec["label"].upper()
        if rec["script"] is not None:
            step = min(rec["step"], len(rec["script"]) - 1)
            return "%s %d/%d" % (PROMPTS[rec["script"][step][0]], step + 1, len(rec["script"]))
        if rec["label"] in ("throw", "drop"):
            return "%s %d/%d" % (label, rec["flights"], rec["takes"])
        secs = (rec["rows"][-1][0] - rec["rows"][0][0]) // 1000 if len(rec["rows"]) > 1 else 0
        return "%s %ds" % (label, secs)

    # --- UI ----------------------------------------------------------------
    def status(self):
        rec = self.recording
        pend = self.pending
        now = time.time()
        return {
            "type": "status",
            "badges": sorted(
                ({**b, "age": round(now - b["seen"], 1)} for b in self.badges.values()),
                key=lambda b: b["name"],
            ),
            "connected": self.address if self.client is not None else None,
            "connecting": self.connecting,
            "info": self.info,
            "lost": self.lost,
            "recording": None
            if rec is None
            else {
                "label": rec["label"],
                "takes": rec["takes"],
                "flights": rec["flights"],
                "marks": len(rec["marks"]),
                "samples": len(rec["rows"]),
                "seconds": round((rec["rows"][-1][0] - rec["rows"][0][0]) / 1000, 1) if len(rec["rows"]) > 1 else 0,
                "prompt": None
                if rec["script"] is None
                else PROMPTS[rec["script"][min(rec["step"], len(rec["script"]) - 1)][0]],
                "step": rec["step"],
                "steps": None if rec["script"] is None else len(rec["script"]),
                "missed": rec["missed"],
            },
            "pending": None
            if pend is None
            else {
                k: pend[k] for k in ("label", "takes", "flights", "duration_s", "rate_hz", "lost_packets", "reason", "missed")
            }
            | {"marks": len(pend["marks"]), "samples": len(pend["rows"])},
            "counts": session_counts(),
        }

    async def broadcast(self, msg):
        text = json.dumps(msg)
        for ws in list(self.sockets):
            try:
                await ws.send_str(text)
            except Exception:
                self.sockets.discard(ws)

    async def broadcast_status(self):
        await self.broadcast(self.status())

    async def pump(self):
        last_status = 0
        while True:
            await asyncio.sleep(0.05)
            if self.live:
                batch, self.live = self.live, []
                await self.broadcast({"type": "samples", "data": batch})
            now = time.time()
            if self.should_reconnect(now):
                self.loop.create_task(self.auto_reconnect())
            if now - last_status >= 0.5:
                last_status = now
                await self.broadcast_status()
                prompt = self.badge_prompt()
                if self.client is not None and prompt != self.sent_prompt:
                    self.sent_prompt = prompt
                    try:
                        await self.command(CMD_LABEL, prompt)
                    except Exception:
                        pass

    async def handle(self, msg):
        op = msg.get("op")
        if op == "connect":
            await self.connect(msg["address"])
        elif op == "disconnect":
            await self.disconnect()
        elif op == "identify":
            await self.command(CMD_IDENTIFY)
        elif op == "rename":
            name = msg["name"].strip()[:16]
            await self.command(CMD_NAME, name)
            if self.address in self.badges:
                self.badges[self.address]["name"] = name
        elif op == "start":
            self.start_recording(msg["label"], int(msg.get("takes", 10)), bool(msg.get("plushy")), msg.get("notes", ""))
        elif op == "mark":
            self.mark()
        elif op == "stop":
            self.finish_recording()
        elif op == "save":
            path = self.save_pending()
            await self.broadcast({"type": "saved", "path": path})
        elif op == "discard":
            self.pending = None
        await self.broadcast_status()


async def ws_handler(request):
    rec = request.app["rec"]
    ws = web.WebSocketResponse(heartbeat=10)
    await ws.prepare(request)
    rec.sockets.add(ws)
    await ws.send_str(json.dumps({"type": "hello", "labels": SESSION_TYPES}))
    await ws.send_str(json.dumps(rec.status()))
    async for m in ws:
        if m.type != WSMsgType.TEXT:
            continue
        try:
            await rec.handle(json.loads(m.data))
        except Exception as e:
            await ws.send_str(json.dumps({"type": "error", "message": str(e)}))
    rec.sockets.discard(ws)
    return ws


async def index(_request):
    # no-store so a restarted recorder's page changes show on a normal reload
    html = (WEB_DIR / "recorder.html").read_text(encoding="utf-8")
    return web.Response(text=html, content_type="text/html", headers={"Cache-Control": "no-store"})


async def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    rec = Recorder()
    rec.loop = asyncio.get_running_loop()
    app = web.Application()
    app["rec"] = rec
    app.router.add_get("/", index)
    app.router.add_get("/ws", ws_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", args.port).start()
    url = "http://localhost:%d/" % args.port
    print("Recorder running at", url, "(Ctrl+C to stop)")
    if not args.no_browser:
        webbrowser.open(url)

    async with BleakScanner(rec.on_adv, scanning_mode="active"):
        try:
            await rec.pump()
        finally:
            if rec.client is not None:
                await rec.client.disconnect()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
