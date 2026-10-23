"""Drive a guided mixed session through the recorder and train from it."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import protocol
import race_rec
from train import train

SCRIPT = [("still", 4000), ("hold", 3000), ("throw", None), ("hold", 3000), ("drop", None), ("still", 4000)]


def sample(rec, t_step):
    """IMU sample for the active prompt, t_step ms after the step began."""
    label = rec.recording["script"][rec.recording["step"]][0]
    az, ax = 16384, 0
    if label == "hold":
        ax = (t_step // 100) % 2 * 3000
    elif label == "throw" and 500 <= t_step < 1100:
        az = 0
    elif label == "drop" and 300 <= t_step < 700:
        az = 0
    return ax, 0, az


class GuidedSessionTests(unittest.TestCase):
    def test_guided_session_labels_segments_and_trains(self):
        for kind in race_rec.GUIDED:
            with self.subTest(kind=kind):
                self.run_guided(kind)

    def test_landings_script_only_throws(self):
        script = race_rec.landings_script(5)
        self.assertEqual(sorted(label for label, _ in script), ["drop"] * 5 + ["throw"] * 5)

    def run_guided(self, kind):
        rec = race_rec.Recorder()
        rec.start_recording(kind, 1, True, "")
        rec.recording["script"] = list(SCRIPT)
        t_ms, seq, step, step_t0 = 0, 0, 0, 0
        while rec.recording is not None and t_ms < 60000:
            if rec.recording["step"] != step:
                step, step_t0 = rec.recording["step"], t_ms
            ax, ay, az = sample(rec, t_ms - step_t0)
            packet = protocol.pack_samples(seq, [protocol.pack_sample(t_ms, ax, ay, az, 0, 0, 0, 0, 0)])
            rec.on_stream(None, packet)
            t_ms += 20
            seq += 1

        self.assertIsNone(rec.recording)
        self.assertEqual(rec.pending["reason"], "script complete")
        self.assertEqual(rec.pending["flights"], 2)
        self.assertEqual([seg["label"] for seg in rec.pending["segments"]], [s for s, _ in SCRIPT])

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with mock.patch.object(race_rec, "DATA_DIR", root / "data"), mock.patch.object(race_rec, "ROOT", root):
                saved = rec.save_pending()
            self.assertTrue(saved.startswith("data/%s/" % kind))
            meta = json.loads((root / saved).with_suffix(".json").read_text())
            self.assertEqual(len(meta["segments"]), len(SCRIPT))
            report = train(root / "data", root / "dist" / "model.py")
            self.assertEqual(set(report["labels"]), {"still", "throw"})
            self.assertEqual(report["landing"]["landings"], {"caught": 1, "dropped": 1})


class ReconnectTests(unittest.TestCase):
    def test_reconnects_only_after_fresh_advert_from_last_badge(self):
        rec = race_rec.Recorder()
        self.assertFalse(rec.should_reconnect(100.0))  # never connected

        rec.reconnect_to = "AA"
        rec.badges["AA"] = {"seen": 100.0}
        rec.disconnected_at = 101.0
        self.assertFalse(rec.should_reconnect(102.0))  # advert predates the drop

        rec.badges["AA"]["seen"] = 103.0
        self.assertTrue(rec.should_reconnect(103.5))

        rec.next_reconnect = 106.0
        self.assertFalse(rec.should_reconnect(104.0))  # backing off after a failure

        rec.client = object()
        self.assertFalse(rec.should_reconnect(110.0))  # already connected

    def test_user_disconnect_disables_reconnect(self):
        rec = race_rec.Recorder()
        rec.reconnect_to = "AA"
        race_rec.asyncio.run(rec.disconnect())
        self.assertIsNone(rec.reconnect_to)


if __name__ == "__main__":
    unittest.main()
