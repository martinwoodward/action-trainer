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
    CMD_MODEL_BEGIN,
    CMD_MODEL_DATA,
    CMD_MODEL_END,
    CMD_NAME,
    COMPANY_ID,
    CONTROL_MAX,
    INFO_UUID,
    STREAM_UUID,
    CONTROL_UUID,
    model_checksum,
    unpack_mfg,
    unpack_samples,
)
import train as trainer

DATA_DIR = ROOT / "data"
WEB_DIR = ROOT / "web"
LABELS = ("throw", "drop", "still", "walk", "unknown")
SEGMENT_LABELS = LABELS + ("ignore",)
STATE_NAMES = ("STILL", "WALK", "FLYING", "THROW", "DROP", "HANDLING")
FLYING = 2
LSB_PER_G = 16384.0
CSV_HEADER = ("t_ms", "ax", "ay", "az", "gx", "gy", "gz", "events", "state")


def session_counts():
    counts = {}
    for label in LABELS:
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
        self.last_seq = None
        self.lost = 0
        self.t_last16 = None
        self.t_ms = 0
        self.recording = None  # dict while recording
        self.pending = None  # finished recording awaiting save/discard
        self.review_ready = False  # pending recording not yet sent to the UI
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
        if self.recording is not None:
            self.finish_recording(reason="disconnected")
        self.loop.create_task(self.broadcast_status())

    async def disconnect(self):
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

    async def deploy_model(self, source):
        if self.client is None:
            raise RuntimeError("connect a badge to deploy")
        chunk = max(20, min(CONTROL_MAX, self.client.mtu_size - 3) - 1)
        write = lambda data: self.client.write_gatt_char(CONTROL_UUID, data, response=True)
        await write(CMD_MODEL_BEGIN)
        for i in range(0, len(source), chunk):
            await write(CMD_MODEL_DATA + source[i:i + chunk])
        tag = "%04x" % model_checksum(source)
        await write(CMD_MODEL_END + f"{len(source)},{model_checksum(source)}".encode())
        for _ in range(20):
            await asyncio.sleep(0.25)
            self.info = (await self.client.read_gatt_char(INFO_UUID)).decode(errors="replace")
            if f"model={tag}" in self.info:
                return tag
            if "model=err" in self.info:
                break
        raise RuntimeError("badge didn't accept the model (is the pedometer app up to date?)")

    async def train_and_deploy(self):
        report = await asyncio.to_thread(trainer.train, DATA_DIR, ROOT / "dist" / "model.py")
        v = report["grouped_validation"]
        result = {
            "type": "trained",
            "windows": report["windows_per_label"],
            "accuracy": v["accuracy"],
            "deployed": None,
        }
        await self.broadcast(result)
        result["deployed"] = await self.deploy_model((ROOT / "dist" / "model.py").read_bytes())
        await self.broadcast(result)

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
            if rec is not None:
                rec["rows"].append(row)
                if state == FLYING and rec["prev_state"] != FLYING:
                    rec["flights"] += 1
                rec["prev_state"] = state
            mag = math.sqrt(ax * ax + ay * ay + az * az) / LSB_PER_G
            self.live.append((self.t_ms, round(mag, 3), state))

    # --- recording ---------------------------------------------------------
    def start_recording(self, label, takes, plushy, notes):
        if label not in LABELS:
            raise RuntimeError("unknown label")
        self.pending = None
        self.recording = {
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
        self.pending = rec
        self.review_ready = True

    def review(self):
        """Whole pending session as (t_ms from start, |a| in g, state) for annotation."""
        rec = self.pending
        if rec is None or not rec["rows"]:
            return None
        t0 = rec["rows"][0][0]
        data = [
            (row[0] - t0, round(math.sqrt(row[1] ** 2 + row[2] ** 2 + row[3] ** 2) / LSB_PER_G, 3), row[8])
            for row in rec["rows"]
        ]
        return {"type": "review", "label": rec["label"], "duration_ms": data[-1][0], "data": data}

    @staticmethod
    def clean_segments(segments, duration_ms):
        out = []
        for seg in segments or ():
            label = str(seg.get("label", "")).lower()
            if label not in SEGMENT_LABELS:
                raise RuntimeError("unknown segment label: %s" % label)
            start = max(0, int(seg["start_ms"]))
            end = min(duration_ms, int(seg["end_ms"]))
            if end > start:
                out.append({"start_ms": start, "end_ms": end, "label": label})
        out.sort(key=lambda s: s["start_ms"])
        for a, b in zip(out, out[1:]):
            if b["start_ms"] < a["end_ms"]:
                raise RuntimeError("segments overlap")
        return out

    def save_pending(self, segments=None):
        rec = self.pending
        if rec is None or not rec["rows"]:
            self.pending = None
            return None
        t0 = rec["rows"][0][0]
        segments = self.clean_segments(segments, rec["rows"][-1][0] - t0)
        self.pending = None
        badge = self.badges.get(self.address, {})
        stamp = rec["started"].strftime("%Y%m%d-%H%M%S")
        folder = DATA_DIR / rec["label"]
        folder.mkdir(parents=True, exist_ok=True)
        stem = "%s_%s" % (stamp, badge.get("badge_id", "unknown"))
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
            "segments": segments,
            "plushy": rec["plushy"],
            "notes": rec["notes"],
            "lost_packets": rec["lost_packets"],
            "end_reason": rec["reason"],
            "lsb_per_g": LSB_PER_G,
        }
        (folder / (stem + ".json")).write_text(json.dumps(meta, indent=2))
        return str((folder / (stem + ".csv")).relative_to(ROOT))

    def badge_prompt(self):
        rec = self.recording
        if rec is None:
            return ""
        label = rec["label"].upper()
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
            },
            "pending": None
            if pend is None
            else {
                k: pend[k] for k in ("label", "takes", "flights", "duration_s", "rate_hz", "lost_packets", "reason")
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
        last_prompt = None
        while True:
            await asyncio.sleep(0.05)
            if self.live:
                batch, self.live = self.live, []
                await self.broadcast({"type": "samples", "data": batch})
            if self.review_ready:
                self.review_ready = False
                msg = self.review()
                if msg:
                    await self.broadcast(msg)
            now = time.time()
            if now - last_status >= 0.5:
                last_status = now
                await self.broadcast_status()
                prompt = self.badge_prompt()
                if self.client is not None and prompt != last_prompt:
                    last_prompt = prompt
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
            path = self.save_pending(msg.get("segments"))
            await self.broadcast({"type": "saved", "path": path})
        elif op == "discard":
            self.pending = None
        elif op == "train_deploy":
            await self.train_and_deploy()
        await self.broadcast_status()


async def ws_handler(request):
    rec = request.app["rec"]
    ws = web.WebSocketResponse(heartbeat=10)
    await ws.prepare(request)
    rec.sockets.add(ws)
    await ws.send_str(json.dumps({"type": "hello", "labels": LABELS}))
    await ws.send_str(json.dumps(rec.status()))
    review = rec.review()
    if review:
        await ws.send_str(json.dumps(review))
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
    return web.FileResponse(WEB_DIR / "recorder.html")


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
