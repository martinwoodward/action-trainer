import ast
import importlib.util
import tempfile
import unittest
from pathlib import Path

from sklearn.tree import DecisionTreeClassifier

from race_features import FEATURE_NAMES, extract_features
from race_train import train, window_label, write_model


class FeatureTests(unittest.TestCase):
    def test_resting_window_has_expected_gravity_and_low_variance(self):
        rows = [(0, 0, 16384, 3, 0, 0)] * 20
        values = extract_features(rows)
        self.assertEqual(len(values), len(FEATURE_NAMES))
        self.assertAlmostEqual(values[0], 1.0)
        self.assertAlmostEqual(values[1], 0.0)
        self.assertAlmostEqual(values[2], 1.0)
        self.assertAlmostEqual(values[3], 1.0)

    def test_empty_window_is_rejected(self):
        with self.assertRaises(ValueError):
            extract_features([])


class TrainingTests(unittest.TestCase):
    def test_generated_model_defaults_unknown_and_empty_windows_to_still(self):
        model = DecisionTreeClassifier(random_state=0)
        model.fit([[0.0] * len(FEATURE_NAMES), [1.0] * len(FEATURE_NAMES)], ["unknown", "throw"])
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "model.py"
            write_model(model, path)
            spec = importlib.util.spec_from_file_location("generated_model", path)
            generated = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(generated)
            self.assertEqual(generated.predict_window([]), "still")
            self.assertEqual(generated.predict_features([0.0] * len(FEATURE_NAMES)), "still")
            self.assertEqual(generated.predict_features([1.0] * len(FEATURE_NAMES)), "throw")
            # MicroPython only allows starred expressions in calls and assignments.
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
                    self.assertFalse(any(isinstance(e, ast.Starred) for e in node.elts))
            features = extract_features([(100, -200, 16000, 30, -40, 50), (300, 0, 15000, 0, 80, -20)])
            self.assertEqual(generated.features_from_window([(100, -200, 16000, 30, -40, 50), (300, 0, 15000, 0, 80, -20)]), features)

    def test_training_exports_self_contained_model_and_validation_report(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            data = root / "data"
            output = root / "dist" / "model.py"
            # Two separate sessions per class make grouped validation eligible.
            for label in ("still", "throw"):
                folder = data / label
                folder.mkdir(parents=True)
                for session in range(2):
                    path = folder / f"session-{session}.csv"
                    with path.open("w", encoding="utf-8") as stream:
                        stream.write("t_ms,ax,ay,az,gx,gy,gz,events,state\n")
                        for t_ms in range(0, 2500, 20):
                            az = 0 if label == "throw" and 1000 <= t_ms < 1500 else 16384
                            stream.write(f"{t_ms},0,0,{az},0,0,0,0,0\n")
            report = train(data, output)
            self.assertTrue(output.is_file())
            self.assertTrue(output.with_name("report.json").is_file())
            self.assertEqual(report["grouped_validation"]["status"], "available")
            self.assertEqual(report["default_action"], "still")

    def test_only_airborne_windows_keep_throw_or_drop_label(self):
        held = [(t, (0, 0, 16384, 0, 0, 0)) for t in range(0, 1000, 20)]
        flight = [(t, (0, 0, 16384 if t < 500 or t >= 800 else 0, 0, 0, 0)) for t in range(0, 1000, 20)]
        clipped = [(t, (0, 0, 0 if t >= 940 else 16384, 0, 0, 0)) for t in range(0, 1000, 20)]
        self.assertEqual(window_label("throw", held), "still")
        self.assertEqual(window_label("throw", flight), "throw")
        self.assertEqual(window_label("drop", flight), "throw")
        self.assertIsNone(window_label("drop", clipped))
        self.assertEqual(window_label("walk", flight), "still")

    def test_tracker_reports_drop_only_after_a_throw_lands_on_the_floor(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            # Each session: rest, a 500 ms flight, then a still (floor) or
            # wobbling (hand) landing.
            for label in ("throw", "drop"):
                folder = root / "data" / label
                folder.mkdir(parents=True)
                for session in range(2):
                    with (folder / f"s{session}.csv").open("w", encoding="utf-8") as stream:
                        stream.write("t_ms,ax,ay,az,gx,gy,gz,events,state\n")
                        # Enough drops that the shrunk leaf score clears DROP_CONFIDENCE.
                        for t_ms, axes in synthetic_flights(label == "drop", flights=10):
                            stream.write("%d,%d,%d,%d,%d,%d,%d,0,0\n" % ((t_ms,) + axes))
            output = root / "dist" / "model.py"
            report = train(root / "data", output)
            self.assertEqual(report["landing"]["landings"], {"caught": 20, "dropped": 20})
            spec = importlib.util.spec_from_file_location("tracked_model", output)
            generated = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(generated)
            self.assertEqual(generated.LABELS, ("still", "throw", "drop"))

            for dropped, expected in ((True, ["throw", "drop", "still"]), (False, ["throw", "still"])):
                tracker = generated.Tracker()
                states = []
                for t_ms, axes in synthetic_flights(dropped, flights=1, pickup=True):
                    state = tracker.update(t_ms, axes)
                    if not states or states[-1] != state:
                        states.append(state)
                self.assertEqual(states[1:], expected)

            # Lying still from the start is never a drop.
            tracker = generated.Tracker()
            for t_ms in range(0, 5000, 20):
                self.assertEqual(tracker.update(t_ms, (0, 0, 16384, 500, -300, 200)), "still")


def synthetic_flights(dropped, flights, pickup=False):
    """Yield (t_ms, axes): 1 s rest, then per flight 500 ms free fall + 3 s landing."""
    t_ms = 0
    for _ in range(50):
        yield t_ms, (0, 0, 16384, 500, -300, 200)
        t_ms += 20
    for _ in range(flights):
        for _ in range(25):
            yield t_ms, (0, 0, 0, 4000, 0, 0)
            t_ms += 20
        for step in range(150):
            if dropped:
                # Resting on the floor: only the gyro's constant offset.
                axes = (0, 0, 16384, 500, -300, 200)
            else:
                wobble = 3000 if (step // 5) % 2 else -3000
                axes = (800, 0, 16384, 500 + wobble, -300, 200 - wobble)
            yield t_ms, axes
            t_ms += 20
    if pickup:
        for _ in range(50):
            yield t_ms, (6000, 0, 16384, 6000, 0, 0)
            t_ms += 20


if __name__ == "__main__":
    unittest.main()
