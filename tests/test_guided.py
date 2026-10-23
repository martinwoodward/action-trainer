"""Guided runs: prompts become tagged segments and the session saves itself."""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path

import recorder
from recorder import GUIDE_REACTION_MS, Recorder


def feed(rec, start_ms, end_ms):
    for t in range(start_ms, end_ms, 20):
        rec.t_ms = t
        rec.recording["rows"].append((t, 0, 0, 16384, 0, 0, 0, 0, 0))


class GuidedTests(unittest.TestCase):
    def test_phases_become_segments_skipping_reaction_time(self):
        phases = [
            {"t_ms": 1000, "label": None},
            {"t_ms": 4000, "label": "walk"},
            {"t_ms": 19000, "label": "unknown"},
            {"t_ms": 19200, "label": "still"},
        ]
        segs = Recorder.phase_segments(phases, 1000, 22000)
        self.assertEqual(segs, [
            {"start_ms": 3000 + GUIDE_REACTION_MS, "end_ms": 18000, "label": "walk"},
            {"start_ms": 18200 + GUIDE_REACTION_MS, "end_ms": 22000, "label": "still"},
        ])

    def test_guided_stop_saves_with_auto_tags(self):
        with tempfile.TemporaryDirectory() as temp:
            data = Path(temp) / "data"
            old = recorder.DATA_DIR, recorder.ROOT
            recorder.DATA_DIR, recorder.ROOT = data, Path(temp)
            try:
                rec = Recorder()
                sent = []

                async def capture(msg):
                    sent.append(msg)

                rec.broadcast = capture

                async def run():
                    await rec.handle({"op": "start"})
                    await rec.handle({"op": "phase", "label": None, "cue": "READY", "ms": 3000})
                    feed(rec, 0, 3000)
                    await rec.handle({"op": "phase", "label": "walk", "cue": "WALK", "ms": 5000})
                    self.assertEqual(rec.badge_prompt(), "WALK 5")
                    feed(rec, 3000, 8000)
                    await rec.handle({"op": "stop"})

                asyncio.run(run())
                saved = [m for m in sent if m.get("type") == "saved"]
                self.assertEqual(len(saved), 1)
                self.assertAlmostEqual(saved[0]["tagged"]["walk"], 4.0, places=2)
                self.assertIsNone(rec.pending)
                meta = json.loads(next((data / "recordings").glob("*.json")).read_text())
                self.assertEqual(meta["segments"][0]["label"], "walk")
                self.assertEqual(len(meta["guided"]), 2)
            finally:
                recorder.DATA_DIR, recorder.ROOT = old

    def test_cancel_discards_guided_run(self):
        rec = Recorder()

        async def noop(msg):
            pass

        rec.broadcast = noop

        async def run():
            await rec.handle({"op": "start"})
            feed(rec, 0, 2000)
            await rec.handle({"op": "cancel"})

        asyncio.run(run())
        self.assertIsNone(rec.recording)
        self.assertIsNone(rec.pending)


if __name__ == "__main__":
    unittest.main()
