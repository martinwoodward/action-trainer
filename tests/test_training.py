import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

from sklearn.tree import DecisionTreeClassifier

from features import FEATURE_NAMES, extract_features
from train import (assign_splits, label_seconds, load_dataset, read_segments, read_session, score_model,
                   train, window_label, window_session, write_model)


class FeatureTests(unittest.TestCase):
    def test_resting_window_has_expected_gravity_and_low_variance(self):
        rows = [(0, 0, 16384, 3, 0, 0)] * 20
        values = extract_features(rows)
        self.assertEqual(len(values), len(FEATURE_NAMES))
        self.assertAlmostEqual(values[0], 1.0)
        self.assertAlmostEqual(values[1], 0.0)
        self.assertAlmostEqual(values[2], 1.0)
        self.assertAlmostEqual(values[3], 1.0)

    def test_rhythm_features_separate_periodic_motion_from_noise_free_rest(self):
        names = list(FEATURE_NAMES)
        still = extract_features([(0, 0, 16384, 0, 0, 0)] * 45)
        # Two clean 0.5 g bumps along gravity, half a window apart.
        walk = [(0, 0, int(16384 * (1 + 0.5 * (i % 22 < 5))), 0, 0, 0) for i in range(45)]
        stepping = extract_features(walk)
        self.assertEqual(still[names.index("accel_peaks")], 0)
        self.assertEqual(stepping[names.index("accel_peaks")], 3)
        self.assertGreater(stepping[names.index("accel_autocorr")], 0.5)
        self.assertGreater(stepping[names.index("accel_vertical_std")], 0.1)
        self.assertAlmostEqual(stepping[names.index("accel_horizontal_std")], 0.0)

    def test_empty_window_is_rejected(self):
        with self.assertRaises(ValueError):
            extract_features([])


class SegmentTests(unittest.TestCase):
    def test_segments_override_session_label_and_skip_straddling_windows(self):
        segments = [(2000, 4000, "unknown"), (6000, 7000, "ignore")]
        self.assertEqual(window_label(0, 1000, "walk", segments), "walk")
        self.assertEqual(window_label(2000, 3000, "walk", segments), "unknown")
        self.assertEqual(window_label(3000, 4000, "walk", segments), "unknown")
        self.assertIsNone(window_label(1500, 2500, "walk", segments))
        self.assertIsNone(window_label(6000, 7000, "walk", segments))
        self.assertIsNone(window_label(5500, 6500, "walk", segments))
        self.assertEqual(window_label(7000, 8000, "walk", segments), "walk")

    def test_session_windows_use_sidecar_segments(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "walk" / "s.csv"
            path.parent.mkdir()
            with path.open("w", encoding="utf-8") as stream:
                stream.write("t_ms,ax,ay,az,gx,gy,gz,events,state\n")
                for t_ms in range(0, 5000, 20):
                    stream.write(f"{t_ms},0,0,16384,0,0,0,0,0\n")
            path.with_suffix(".json").write_text(
                json.dumps({"segments": [{"start_ms": 2000, "end_ms": 4000, "label": "unknown"}]}))
            _, y, _ = window_session(read_session(path), "walk", "g", read_segments(path))
            self.assertIn("unknown", y)
            self.assertIn("walk", y)
            self.assertEqual(y.count("unknown"), 5)

    def _write_session(self, folder, name, duration_ms, segments, duration_s):
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{name}.csv"
        with path.open("w", encoding="utf-8") as stream:
            stream.write("t_ms,ax,ay,az,gx,gy,gz,events,state\n")
            for t_ms in range(0, duration_ms, 20):
                stream.write(f"{t_ms},0,0,16384,0,0,0,0,0\n")
        path.with_suffix(".json").write_text(json.dumps({"duration_s": duration_s, "segments": segments}))

    def test_recordings_train_only_tagged_time(self):
        with tempfile.TemporaryDirectory() as temp:
            data = Path(temp)
            self._write_session(data / "recordings", "a", 8000,
                                [{"start_ms": 1000, "end_ms": 3000, "label": "walk"},
                                 {"start_ms": 4000, "end_ms": 6000, "label": "still"}], 8)
            _, y, _, sessions, _ = load_dataset(data)
            self.assertEqual(set(y), {"walk", "still"})
            self.assertEqual(y.count("walk"), 5)
            self.assertEqual(sessions, {"walk": 1, "still": 1})

    def test_label_seconds_counts_tags_and_legacy_untagged_time(self):
        with tempfile.TemporaryDirectory() as temp:
            data = Path(temp)
            self._write_session(data / "recordings", "a", 1000,
                                [{"start_ms": 0, "end_ms": 2500, "label": "throw"},
                                 {"start_ms": 3000, "end_ms": 4000, "label": "ignore"}], 10)
            self._write_session(data / "walk", "b", 1000,
                                [{"start_ms": 0, "end_ms": 1000, "label": "unknown"}], 6)
            seconds = label_seconds(data)
            self.assertEqual(seconds["throw"], 2.5)
            self.assertEqual(seconds["walk"], 5.0)
            self.assertEqual(seconds["unknown"], 1.0)
            self.assertEqual(seconds["still"], 0.0)


class TrainingTests(unittest.TestCase):
    def test_generated_model_defaults_unknown_and_empty_windows_to_still(self):
        model = DecisionTreeClassifier(random_state=0)
        model.fit([[0.0] * len(FEATURE_NAMES), [1.0] * len(FEATURE_NAMES)], ["unknown", "walk"])
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "model.py"
            write_model(model, path)
            spec = importlib.util.spec_from_file_location("generated_model", path)
            generated = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(generated)
            self.assertEqual(generated.predict_window([]), "still")
            self.assertEqual(generated.predict_features([0.0] * len(FEATURE_NAMES)), "still")
            self.assertEqual(generated.predict_features([1.0] * len(FEATURE_NAMES)), "walk")

    def test_generated_model_features_match_training_features(self):
        model = DecisionTreeClassifier(random_state=0)
        model.fit([[0.0] * len(FEATURE_NAMES), [1.0] * len(FEATURE_NAMES)], ["still", "walk"])
        window = [(i * 37 % 900, -i * 11, 16000 + (i * 997 % 5000), i, -2 * i, 3) for i in range(44)]
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "model.py"
            write_model(model, path)
            scope = {}
            exec(path.read_text(), scope)
            self.assertNotIn("import features", path.read_text())
            self.assertEqual(scope["FEATURE_NAMES"], FEATURE_NAMES)
            self.assertEqual(scope["features_from_window"](window), extract_features(window))
            self.assertIsNone(scope["features_from_window"]([]))

    def test_generated_model_avoids_star_unpacking_unsupported_by_micropython(self):
        model = DecisionTreeClassifier(random_state=0)
        model.fit([[0.0] * len(FEATURE_NAMES), [1.0] * len(FEATURE_NAMES)], ["unknown", "walk"])
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "model.py"
            write_model(model, path)
            self.assertNotIn("*(", path.read_text())

    def test_training_exports_self_contained_model_and_validation_report(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data = root / "data"
            output = root / "dist" / "model.py"
            # Two separate sessions per class make grouped validation eligible.
            for label_index, label in enumerate(("still", "walk")):
                folder = data / label
                folder.mkdir(parents=True)
                for session in range(2):
                    path = folder / f"session-{session}.csv"
                    with path.open("w", encoding="utf-8") as stream:
                        stream.write("t_ms,ax,ay,az,gx,gy,gz,events,state\n")
                        for t_ms in range(0, 2500, 20):
                            # The changing X-axis signal separates the classes.
                            ax = 0 if label_index == 0 else ((t_ms // 100) % 2) * 8000
                            stream.write(f"{t_ms},{ax},0,16384,0,0,0,0,0\n")
            report = train(data, output)
            self.assertTrue(output.is_file())
            self.assertTrue(output.with_name("report.json").is_file())
            self.assertEqual(report["grouped_validation"]["status"], "available")
            self.assertEqual(report["default_action"], "still")
            self.assertEqual(report["test"]["recordings"], ["walk/session-1"])

    def test_splits_hold_out_every_fourth_recording_and_never_move(self):
        with tempfile.TemporaryDirectory() as temp:
            first = assign_splits(temp, [f"r/2026-{i:02}" for i in range(6)])
            self.assertEqual([g for g, role in sorted(first.items()) if role == "test"], ["r/2026-03"])
            # A recording that sorts earlier doesn't reshuffle earlier roles.
            later = assign_splits(temp, list(first) + ["w/2025-99", "r/2026-06"])
            self.assertEqual({g: later[g] for g in first}, first)
            self.assertEqual(sum(role == "test" for role in later.values()), 2)

    def test_score_model_reports_fake_and_missed_steps(self):
        source = "def predict_window(samples):\n    return 'walk' if samples[0][0] else 'still'\n"
        moving = [(1, 0, 0, 0, 0, 0)]
        resting = [(0, 0, 0, 0, 0, 0)]
        score = score_model(source, [moving, resting, moving, resting],
                            ["walk", "walk", "unknown", "still"])
        self.assertEqual(score["accuracy"], 0.5)
        self.assertEqual(score["recall"]["walk"], 0.5)
        self.assertEqual(score["missed_steps"], 0.5)
        self.assertEqual(score["fake_steps"], 0.5)


if __name__ == "__main__":
    unittest.main()
