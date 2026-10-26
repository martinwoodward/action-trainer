"""Training-data recorder for badge motion models.

Runs a local web UI (http://localhost:8765) backed by bleak. Pick a badge,
connect over GATT, and record whatever movement you like; the badge streams
every IMU sample (with its detector state). Tag stretches of the recording as
walk/throw/drop/still/other, and it is saved to
data/recordings/<timestamp>_<badge>.csv plus a .json sidecar.

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
    CMD_MODEL_SHOW,
    CMD_NAME,
    CMD_PING,
    COMPANY_ID,
    CONTROL_MAX,
    INFO_UUID,
    PING_INTERVAL_MS,
    STREAM_UUID,
    CONTROL_UUID,
    model_checksum,
    unpack_mfg,
    unpack_samples,
)
import train as trainer

LOG_PATH = Path(__file__).parent / "data" / "recorder.log"
RETRY_MIN_S = 2
RETRY_MAX_S = 15


def log(*parts):
    line = datetime.now().strftime("%H:%M:%S ") + " ".join(str(p) for p in parts)
    print(line)
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def describe(e):
    return str(e) or type(e).__name__

DATA_DIR = ROOT / "data"
WEB_DIR = ROOT / "web"
DIST_DIR = ROOT / "dist"
MODEL_PATH = DIST_DIR / "model.py"
# The last model that reached a badge, and its test-set scorecard.
DEPLOYED_MODEL = DIST_DIR / "deployed_model.py"
DEPLOYED_CARD = DIST_DIR / "deployed.json"
MODEL_HIDDEN = DIST_DIR / "model_hidden"  # older recorders' "hide the model" flag; read once
# Demo reset: whether the model is hidden, and when training last started over.
DEMO_STATE = DIST_DIR / "demo.json"
# Guided runs that, once deployed since the last reset, unlock chart colours and squirrel sounds.
GUIDED_RUNS = ("walk", "throw", "drop")
SOUNDS_DIR = WEB_DIR / "sounds"
LABELS = ("throw", "drop", "still", "walk", "unknown")
SEGMENT_LABELS = LABELS + ("ignore",)
GUIDE_REACTION_MS = 1000  # skip this long after each guided cue while the user reacts
STATE_NAMES = ("STILL", "WALK", "FLYING", "THROW", "DROP", "HANDLING")
FLYING = 2
LSB_PER_G = 16384.0
CSV_HEADER = ("t_ms", "ax", "ay", "az", "gx", "gy", "gz", "events", "state")


def load_demo():
    """(model hidden, reset time) from the demo state file."""
    try:
        state = json.loads(DEMO_STATE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {"hidden": MODEL_HIDDEN.exists()}
    return bool(state.get("hidden")), state.get("reset_at")


def guided_runs(data_dir, since=None):
    """Kinds of guided run (walk, throw, drop) that ran to the end, started at or after `since`."""
    done = set()
    for path in (data_dir / trainer.RECORDINGS).glob("*.json"):
        try:
            meta = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        notes = str(meta.get("notes") or "")
        if (notes.startswith("guided ") and meta.get("end_reason") == "stopped"
                and (since is None or str(meta.get("started") or "") >= since)):
            done.add(notes.split(" ", 1)[1])
    return done


class Recorder:
    def __init__(self):
        self.badges = {}  # address -> dict
        self.devices = {}  # address -> BLEDevice
        self.sockets = set()
        self.client = None
        self.address = None
        # Demo: show "no model" in the UI and on the badge until the next training run.
        self.hide_model, self.reset_at = load_demo()
        self.guided = None  # cached guided runs finished since reset_at; reset on save
        self.target = None  # badge to stay connected to; cleared by an explicit disconnect
        self.retry_at = 0.0
        self.retry_delay = 0.0
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
        self.seconds = None  # cached tagged seconds per label; reset on save
        self.write_lock = asyncio.Lock()  # CoreBluetooth allows one pending write per characteristic

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
        self.target = address
        self.connecting = True
        await self.broadcast_status()
        client = BleakClient(dev, timeout=10, disconnected_callback=self.on_disconnect)
        try:
            await client.connect()
            self.last_seq = None
            self.lost = 0
            self.t_last16 = None
            self.info = (await client.read_gatt_char(INFO_UUID)).decode(errors="replace")
            await client.start_notify(STREAM_UUID, self.on_stream)
            self.client = client
            self.address = address
            self.retry_delay = 0.0
            log("connected", self.info)
            try:
                await self.command(CMD_MODEL_SHOW, "0" if self.hide_model else "1")
            except Exception as e:
                log("couldn't update the badge's model line:", describe(e))
        except Exception as e:
            try:
                await client.disconnect()
            except Exception:
                pass
            raise RuntimeError("couldn't reach the badge, retrying") from e
        finally:
            self.connecting = False
            await self.broadcast_status()

    async def reconnect(self):
        try:
            await self.connect(self.target)
        except Exception as e:
            # Back off so a wedged badge has time to restart its BLE stack.
            self.retry_delay = min(RETRY_MAX_S, max(RETRY_MIN_S, self.retry_delay * 2))
            self.retry_at = time.time() + self.retry_delay
            log("reconnect failed:", describe(e.__cause__ or e), "- retrying in %ds" % self.retry_delay)

    def on_disconnect(self, client):
        self.loop.call_soon_threadsafe(self._handle_disconnect, client)

    def _handle_disconnect(self, client=None):
        if client is not None and client is not self.client:
            return
        if self.client is not None:
            log("badge disconnected")
        self.client = None
        if self.recording is not None:
            self.finish_recording(reason="disconnected")
        self.loop.create_task(self.broadcast_status())

    async def drop_link(self):
        client = self.client
        self._handle_disconnect()
        if client is not None:
            try:
                await asyncio.wait_for(client.disconnect(), 5)
            except Exception:
                pass

    async def disconnect(self):
        self.target = None
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
        await self.write_control(op + arg.encode()[:60])

    async def write_control(self, data):
        async with self.write_lock:
            await asyncio.wait_for(self.client.write_gatt_char(CONTROL_UUID, data, response=True), 5)

    async def deploy_model(self, source):
        if self.client is None:
            raise RuntimeError("connect a badge to deploy")
        chunk = max(20, min(CONTROL_MAX, self.client.mtu_size - 3) - 1)
        write = self.write_control
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

    def deployed_card(self):
        """The last deployed scorecard, if the connected badge is running it."""
        if not DEPLOYED_CARD.is_file():
            return None
        try:
            card = json.loads(DEPLOYED_CARD.read_text(encoding="utf-8"))
        except ValueError:
            return None
        if self.info and f"model={card.get('tag')}" not in self.info.split():
            return None
        return card

    def guided_done(self):
        """Guided runs finished since the last demo reset."""
        if self.guided is None:
            self.guided = guided_runs(DATA_DIR, self.reset_at) & set(GUIDED_RUNS)
        return self.guided

    def model_card(self):
        """Scorecard of the model on the badge. `unlocked` lists the guided runs the deployed model was trained after, which
        turn on the chart colours (walk; throw + drop) and the squirrel sounds."""
        card = self.deployed_card()
        unlocked = [] if self.hide_model or card is None else card.get("unlocked", [])
        return {"deployed": card, "hidden": self.hide_model,
                "unlocked": unlocked, "guided": sorted(self.guided_done())}

    def save_demo(self):
        DEMO_STATE.parent.mkdir(parents=True, exist_ok=True)
        DEMO_STATE.write_text(json.dumps({"hidden": self.hide_model, "reset_at": self.reset_at}), encoding="utf-8")
        MODEL_HIDDEN.unlink(missing_ok=True)

    async def set_model_hidden(self, hidden):
        changed = hidden != self.hide_model
        self.hide_model = hidden
        self.save_demo()
        if changed and self.client is not None:
            try:
                await self.command(CMD_MODEL_SHOW, "0" if hidden else "1")
            except Exception as e:
                log("couldn't update the badge's model line:", describe(e))

    async def reset_demo(self):
        """Back to before any training: hide the model and start counting guided runs again."""
        self.reset_at = datetime.now().isoformat(timespec="seconds")
        self.guided = None
        await self.set_model_hidden(True)
        log("demo reset")

    async def train_and_deploy(self):
        """Train and deploy, scoring the new model against the one already on the
        badge so the UI can show the change. While the model is hidden for a demo,
        don't compare against the hidden model."""
        demo = self.hide_model
        await self.broadcast({"type": "training"})
        baseline_path = DEPLOYED_MODEL if self.deployed_card() else None
        report = await asyncio.to_thread(trainer.train, DATA_DIR, MODEL_PATH, baseline_path)
        test = dict(report["test"] or {})
        baseline = test.pop("baseline", None)
        source = MODEL_PATH.read_bytes()
        tag = await self.deploy_model(source)
        DEPLOYED_MODEL.write_bytes(source)
        DEPLOYED_CARD.write_text(json.dumps({
            "tag": tag,
            "deployed_at": datetime.now().isoformat(timespec="seconds"),
            "test": test or None,
            "previous": None if demo else baseline,
            "windows": report["windows_per_label"],
            "unlocked": sorted(self.guided_done()),
        }, indent=2), encoding="utf-8")
        await self.set_model_hidden(False)
        await self.broadcast({"type": "trained", "deployed": tag, "model": self.model_card()})

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
    def start_recording(self, notes=""):
        self.pending = None
        self.recording = {
            "notes": notes,
            "rows": [],
            "marks": [],
            "phases": [],
            "flights": 0,
            "prev_state": None,
            "started": datetime.now(),
        }

    def mark(self):
        rec = self.recording
        if rec is not None:
            rec["marks"].append(self.t_ms)

    def phase(self, label, cue="", ms=0):
        """Guided runs: the UI switched prompt. Stamp it on the badge clock."""
        rec = self.recording
        if rec is None:
            return
        label = label if label in SEGMENT_LABELS else None
        rec["phases"].append({"t_ms": self.t_ms, "label": label, "cue": str(cue)[:12], "ms": int(ms)})

    @staticmethod
    def phase_segments(phases, t0, end_ms):
        """Turn guided phases into segments, skipping the reaction time after each cue."""
        out = []
        for i, ph in enumerate(phases):
            if not ph["label"]:
                continue
            start = ph["t_ms"] - t0 + GUIDE_REACTION_MS
            stop = (phases[i + 1]["t_ms"] - t0) if i + 1 < len(phases) else end_ms
            stop = min(stop, end_ms)
            if stop - start >= 500:
                out.append({"start_ms": max(0, start), "end_ms": stop, "label": ph["label"]})
        return out

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
        if rec["phases"] and rows:
            rec["auto_segments"] = self.phase_segments(rec["phases"], rows[0][0], rows[-1][0] - rows[0][0])
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
        return {"type": "review", "duration_ms": data[-1][0], "data": data, "segments": rec.get("auto_segments", [])}

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
        folder = DATA_DIR / trainer.RECORDINGS
        folder.mkdir(parents=True, exist_ok=True)
        stem = "%s_%s" % (stamp, badge.get("badge_id", "unknown"))
        with open(folder / (stem + ".csv"), "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(CSV_HEADER)
            for row in rec["rows"]:
                w.writerow((row[0] - t0,) + row[1:])
        meta = {
            "badge_id": badge.get("badge_id"),
            "badge_name": badge.get("name"),
            "badge_info": self.info,
            "started": rec["started"].isoformat(timespec="seconds"),
            "duration_s": rec["duration_s"],
            "samples": len(rec["rows"]),
            "rate_hz": rec["rate_hz"],
            "flights_detected": rec["flights"],
            "marks_ms": [m - t0 for m in rec["marks"]],
            "segments": segments,
            "guided": [{**ph, "t_ms": ph["t_ms"] - t0} for ph in rec["phases"]],
            "notes": rec["notes"],
            "lost_packets": rec["lost_packets"],
            "end_reason": rec["reason"],
            "lsb_per_g": LSB_PER_G,
        }
        (folder / (stem + ".json")).write_text(json.dumps(meta, indent=2))
        self.seconds = None
        self.guided = None
        return str((folder / (stem + ".csv")).relative_to(ROOT))

    def badge_prompt(self):
        rec = self.recording
        if rec is None:
            return ""
        if rec["phases"]:
            ph = rec["phases"][-1]
            left = max(0, ph["t_ms"] + ph["ms"] - self.t_ms + 999) // 1000
            return "%s %d" % (ph["cue"], left) if ph["ms"] else ph["cue"]
        secs = (rec["rows"][-1][0] - rec["rows"][0][0]) // 1000 if len(rec["rows"]) > 1 else 0
        return "REC %ds" % secs

    # --- UI ----------------------------------------------------------------
    def label_seconds(self):
        if self.seconds is None:
            self.seconds = trainer.label_seconds(DATA_DIR)
        return self.seconds

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
            "reconnecting": self.target if self.client is None else None,
            "info": self.info,
            "lost": self.lost,
            "recording": None
            if rec is None
            else {
                "flights": rec["flights"],
                "marks": len(rec["marks"]),
                "samples": len(rec["rows"]),
                "seconds": round((rec["rows"][-1][0] - rec["rows"][0][0]) / 1000, 1) if len(rec["rows"]) > 1 else 0,
            },
            "pending": None
            if pend is None
            else {
                k: pend[k] for k in ("flights", "duration_s", "rate_hz", "lost_packets", "reason")
            }
            | {"marks": len(pend["marks"]), "samples": len(pend["rows"])},
            "seconds": self.label_seconds(),
            "model": self.model_card(),
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

    async def keep_alive(self):
        if self.client is not None:
            try:
                await self.write_control(CMD_PING)
                self.ping_failures = 0
            except Exception:
                self.ping_failures += 1
                if self.ping_failures >= 2:
                    log("badge stopped answering, dropping link")
                    await self.drop_link()
        elif self.target and not self.connecting and time.time() >= self.retry_at:
            b = self.badges.get(self.target)
            if b and time.time() - b["seen"] < 5:
                await self.reconnect()

    async def pump(self):
        last_status = 0
        last_prompt = None
        last_ping = 0
        self.ping_failures = 0
        alive = None
        while True:
            await asyncio.sleep(0.05)
            now = time.time()
            if (alive is None or alive.done()) and now - last_ping >= PING_INTERVAL_MS / 1000:
                last_ping = now
                alive = asyncio.create_task(self.keep_alive())
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
            self.start_recording(msg.get("notes", ""))
        elif op == "mark":
            self.mark()
        elif op == "phase":
            self.phase(msg.get("label"), msg.get("cue", ""), msg.get("ms", 0))
        elif op == "stop":
            self.finish_recording()
            pend = self.pending
            if pend is not None and pend.get("auto_segments"):
                segments = pend["auto_segments"]
                path = self.save_pending(segments)
                self.review_ready = False
                tagged = {}
                for seg in segments:
                    tagged[seg["label"]] = tagged.get(seg["label"], 0) + (seg["end_ms"] - seg["start_ms"]) / 1000
                await self.broadcast({"type": "saved", "path": path, "tagged": tagged})
        elif op == "cancel":
            self.recording = None
            self.pending = None
        elif op == "save":
            path = self.save_pending(msg.get("segments"))
            await self.broadcast({"type": "saved", "path": path})
        elif op == "discard":
            self.pending = None
        elif op == "train_deploy":
            await self.train_and_deploy()
        elif op == "reset_demo":
            await self.reset_demo()
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
    app.router.add_static("/sounds", SOUNDS_DIR)
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
