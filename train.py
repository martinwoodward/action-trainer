"""Train and export a small badge-compatible throw/drop model.

Example: python train.py --data data --output dist/model.py

The export has two stages. A window model labels each second as still or
throw (airborne). When a throw lands, a landing model scores the following
second; the badge reports drop only when that score reaches the drop
confidence, and otherwise assumes the throw was caught.
"""
import argparse
import csv
import json
import math
from collections import Counter, defaultdict
from pathlib import Path

from sklearn.metrics import accuracy_score, confusion_matrix
from sklearn.model_selection import GroupKFold
from sklearn.tree import DecisionTreeClassifier

from features import AXIS_NAMES, FEATURE_NAMES, FREEFALL_G, LSB_PER_G, extract_features

LABELS = ("drop", "still", "throw", "unknown", "walk")
STATES = ("still", "throw", "drop")
DEFAULT_ACTION = "still"
WINDOW_MS = 1000
HOP_MS = 250
MAX_DEPTH = 6
# Throw/drop sessions include handling between takes. Only windows containing
# free fall are airborne ("throw"); windows without any are still/held.
AIRBORNE_LABELS = ("throw", "drop")
MIN_AIRBORNE_MS = 150
# Guided mixed sessions: the JSON sidecar lists timed prompt segments.
MIXED = "mixed"
GUIDED = (MIXED, "landings")  # recorder session types with prompt segments
REACTION_MS = 1000

# Landing stage. A flight ends once no free fall has been seen for
# LANDING_DELAY_MS + LANDING_MS; the landing window is the LANDING_MS that
# start LANDING_DELAY_MS after the last free-fall sample.
LANDING_DELAY_MS = 0
LANDING_MS = 500
LANDING_DEPTH = 2
LANDING_MIN_LEAF = 3
# Leaf scores are shrunk toward "caught": drops / (leaf samples + prior).
LANDING_PRIOR = 2
DROP_CONFIDENCE = 0.85
# Orientation-independent features only. gyro_mag_mean is excluded because it
# includes the gyro's resting offset, which differs per badge.
LANDING_FEATURE_NAMES = (
    "accel_mag_mean",
    "accel_mag_std",
    "accel_mag_min",
    "accel_mag_max",
    "gyro_mag_std",
    "gyro_axis_std",
    "accel_axis_std",
)
LANDING_FEATURES = tuple(FEATURE_NAMES.index(name) for name in LANDING_FEATURE_NAMES)
LANDING_LABELS = {"throw": "caught", "drop": "dropped"}


def read_session(path):
    with path.open(newline="", encoding="utf-8") as stream:
        rows = list(csv.DictReader(stream))
    samples = []
    for row in rows:
        try:
            t_ms = int(row["t_ms"])
            axes = tuple(int(row[name]) for name in AXIS_NAMES)
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError(f"{path}: invalid IMU CSV row: {error}") from error
        samples.append((t_ms, axes))
    return samples


def is_freefall(axes):
    ax, ay, az = axes[:3]
    return math.sqrt(ax * ax + ay * ay + az * az) / LSB_PER_G < FREEFALL_G


def airborne_spans(window):
    return [(t_ms, t_next) for (t_ms, axes), (t_next, _) in zip(window, window[1:]) if is_freefall(axes)]


def airborne_ms(window):
    """Milliseconds of free fall (|a| < FREEFALL_G) in a list of (t_ms, axes)."""
    return sum(t_next - t_ms for t_ms, t_next in airborne_spans(window))


def window_label(label, window):
    """Window-stage label for a single-label session: throw (airborne) or still."""
    if label not in AIRBORNE_LABELS:
        return "still"
    airborne = airborne_ms(window)
    if airborne >= MIN_AIRBORNE_MS:
        return "throw"
    # A window that clips only the edge of a flight is ambiguous; skip it.
    return "still" if airborne == 0 else None


def prompt_at(segments, t_ms):
    for seg in segments:
        if seg["start_ms"] <= t_ms < seg["end_ms"]:
            return seg["label"]
    return None


def mixed_window_label(window, segments):
    """Window-stage label from a guided session's prompt segments.

    Airborne windows are throws when free fall began under a throw or drop
    prompt; free fall during other prompts (handling) is skipped. Grounded
    windows are still.
    """
    spans = airborne_spans(window)
    airborne = sum(end - start for start, end in spans)
    if not airborne:
        return "still"
    if airborne < MIN_AIRBORNE_MS:
        return None
    return "throw" if prompt_at(segments, spans[0][0]) in AIRBORNE_LABELS else None


def window_session(samples, labeler, group):
    if not samples:
        return [], [], []
    x, y, groups = [], [], []
    start = samples[0][0]
    end = samples[-1][0]
    while start + WINDOW_MS <= end:
        window = [sample for sample in samples if start <= sample[0] < start + WINDOW_MS]
        target = labeler(window) if len(window) >= 2 else None
        if target is not None:
            x.append(extract_features([axes for _, axes in window]))
            y.append(target)
            groups.append(group)
        start += HOP_MS
    return x, y, groups


def flights(samples):
    """Group free fall into flights the way the badge does.

    Returns (start_ms, last_freefall_ms, airborne_ms) per flight; free fall
    separated by less than LANDING_DELAY_MS + LANDING_MS (bounces, juggles)
    belongs to the same flight.
    """
    out = []
    for (t_ms, axes), (t_next, _) in zip(samples, samples[1:]):
        if not is_freefall(axes):
            continue
        if out and t_ms - out[-1][1] < LANDING_DELAY_MS + LANDING_MS:
            out[-1][1] = t_ms
            out[-1][2] += t_next - t_ms
        else:
            out.append([t_ms, t_ms, t_next - t_ms])
    return [tuple(flight) for flight in out]


def landing_window(samples, last_freefall):
    start = last_freefall + LANDING_DELAY_MS
    return [axes for t_ms, axes in samples if start <= t_ms < start + LANDING_MS]


def landing_session(samples, label_for_flight, group):
    """Landing features and caught/dropped labels for one session's flights."""
    x, y, groups = [], [], []
    end = samples[-1][0] if samples else 0
    for start, last_freefall, airborne in flights(samples):
        if airborne < MIN_AIRBORNE_MS or last_freefall + LANDING_DELAY_MS + LANDING_MS > end:
            continue
        target = LANDING_LABELS.get(label_for_flight(start))
        window = landing_window(samples, last_freefall)
        if target is None or len(window) < 2:
            continue
        features = extract_features(window)
        x.append([features[i] for i in LANDING_FEATURES])
        y.append(target)
        groups.append(group)
    return x, y, groups


def load_dataset(data_dir):
    """Return window-stage and landing-stage datasets plus per-label counts."""
    windows = ([], [], [])
    landings = ([], [], [])
    sessions_by_label = defaultdict(int)
    samples_by_label = Counter()
    for label_dir in sorted(path for path in data_dir.iterdir() if path.is_dir()):
        label = label_dir.name.lower()
        if label not in LABELS and label not in GUIDED:
            continue
        for path in sorted(label_dir.glob("*.csv")):
            samples = read_session(path)
            group = path.parent.name + "/" + path.stem
            if label in GUIDED:
                segments = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))["segments"]
                labeler = lambda window, segments=segments: mixed_window_label(window, segments)
                flight_label = lambda t_ms, segments=segments: prompt_at(segments, t_ms)
            else:
                labeler = lambda window, label=label: window_label(label, window)
                flight_label = lambda t_ms, label=label: label
            sx, sy, sg = window_session(samples, labeler, group)
            for dataset, part in ((windows, (sx, sy, sg)), (landings, landing_session(samples, flight_label, group))):
                for column, values in zip(dataset, part):
                    column.extend(values)
            if sx:
                sessions_by_label[label] += 1
                samples_by_label[label] += len(sx)
    if not windows[0]:
        raise ValueError(f"No complete 1-second training windows found under {data_dir}")
    return windows, landings, dict(sessions_by_label), dict(samples_by_label)


def window_tree():
    return DecisionTreeClassifier(max_depth=MAX_DEPTH, random_state=42)


def landing_tree():
    return DecisionTreeClassifier(max_depth=LANDING_DEPTH, min_samples_leaf=LANDING_MIN_LEAF, random_state=42)


def grouped_validation(x, y, groups):
    label_list = sorted(set(y))
    group_counts = {label: len({g for g, target in zip(groups, y) if target == label}) for label in label_list}
    if len(group_counts) < 2 or min(group_counts.values()) < 2:
        return {
            "status": "unavailable",
            "reason": "need at least two independent sessions for every trained label",
            "accuracy": None,
            "labels": label_list,
            "confusion_matrix": None,
        }

    folds = min(5, len(set(groups)))
    splitter = GroupKFold(n_splits=folds)
    predictions = [None] * len(y)
    for train_idx, test_idx in splitter.split(x, y, groups):
        train_labels = {y[i] for i in train_idx}
        if train_labels != set(label_list):
            return {
                "status": "unavailable",
                "reason": "a grouped fold omits a class from its training partition",
                "accuracy": None,
                "labels": label_list,
                "confusion_matrix": None,
            }
        model = window_tree()
        model.fit([x[i] for i in train_idx], [y[i] for i in train_idx])
        for i, prediction in zip(test_idx, model.predict([x[j] for j in test_idx])):
            predictions[i] = str(prediction)

    return {
        "status": "available",
        "reason": None,
        "accuracy": accuracy_score(y, predictions),
        "labels": label_list,
        "confusion_matrix": confusion_matrix(y, predictions, labels=label_list).tolist(),
    }


def drop_scores(model, x):
    """Shrunk leaf drop scores, matching the exported _landing()."""
    tree = model.tree_
    dropped = list(model.classes_).index("dropped")
    scores = []
    for leaf in model.apply(x):
        n = int(tree.n_node_samples[leaf])
        drops = round(float(tree.value[leaf][0][dropped]) * n)
        scores.append(drops / (n + LANDING_PRIOR))
    return scores


def landing_validation(x, y, groups, drop_confidence):
    """Leave-one-session-out scores for every landing."""
    if set(y) != {"caught", "dropped"}:
        return {"status": "unavailable", "reason": "need caught and dropped landings"}
    caught = dropped = false_drops = missed_drops = skipped = 0
    majority_false = majority_missed = 0
    for group in sorted(set(groups)):
        train_idx = [i for i, g in enumerate(groups) if g != group]
        test_idx = [i for i, g in enumerate(groups) if g == group]
        if {y[i] for i in train_idx} != {"caught", "dropped"}:
            skipped += len(test_idx)
            continue
        model = landing_tree()
        model.fit([x[i] for i in train_idx], [y[i] for i in train_idx])
        test_x = [x[i] for i in test_idx]
        for i, score, majority in zip(test_idx, drop_scores(model, test_x), model.predict(test_x)):
            is_drop = score >= drop_confidence
            if y[i] == "dropped":
                dropped += 1
                missed_drops += not is_drop
                majority_missed += majority != "dropped"
            else:
                caught += 1
                false_drops += is_drop
                majority_false += majority == "dropped"
    return {
        "status": "available",
        "method": "leave one session out",
        "caught_landings": caught,
        "dropped_landings": dropped,
        "false_drops": false_drops,
        "missed_drops": missed_drops,
        "skipped_landings": skipped,
        # Held-out folds have fewer drops, so shrunk scores fall short of the
        # confidence more often than with the full model; the tree's plain
        # majority shows how separable the landings are.
        "majority_false_drops": majority_false,
        "majority_missed_drops": majority_missed,
    }


def tree_source(model, leaf, feature_index=None):
    tree = model.tree_

    def emit(node, depth):
        indent = "    " * depth
        feature = tree.feature[node]
        if feature < 0:
            return indent + "return " + repr(leaf(node))
        index = feature_index[feature] if feature_index else feature
        lines = [
            f"{indent}if features[{index}] <= {tree.threshold[node]:.17g}:",
            emit(tree.children_left[node], depth + 1),
            f"{indent}else:",
            emit(tree.children_right[node], depth + 1),
        ]
        return "\n".join(lines)

    return emit(0, 1)


def window_source(model):
    classes = [str(value) for value in model.classes_]
    return tree_source(model, lambda node: classes[int(model.tree_.value[node][0].argmax())])


def landing_source(model):
    if model is None:
        return "    return 0.0"
    tree = model.tree_
    dropped = list(model.classes_).index("dropped")

    def leaf(node):
        n = int(tree.n_node_samples[node])
        return round(round(float(tree.value[node][0][dropped]) * n) / (n + LANDING_PRIOR), 4)

    return tree_source(model, leaf, LANDING_FEATURES)


MODEL_TEMPLATE = '''"""Generated by Action Trainer; do not edit by hand.

Tracker turns raw IMU samples into one of STATES at all times: still, throw
(airborne), or drop (a throw that landed on the floor). Drop is reported only
after a throw and only when the landing score reaches DROP_CONFIDENCE;
otherwise the catch is assumed to have succeeded.
"""
import math

MODEL_VERSION = 2
FEATURE_NAMES = %(feature_names)r
LABELS = %(states)r
STATES = LABELS
DEFAULT_ACTION = %(default)r
LSB_PER_G = 16384.0
LSB_PER_DPS = 70.0
FREEFALL_G = %(freefall_g)r
WINDOW_MS = %(window_ms)r
PREDICT_EVERY_MS = 100
LANDING_DELAY_MS = %(landing_delay_ms)r
LANDING_MS = %(landing_ms)r
DROP_CONFIDENCE = %(drop_confidence)r
# Leave drop once it has shown for DROP_HOLD_MS and the badge is picked up.
DROP_HOLD_MS = 1500
PICKUP_G = 0.2
PICKUP_DPS = 40.0


def _mean_std(values):
    n = len(values)
    if not n:
        return 0.0, 0.0
    mean = sum(values) / n
    variance = sum((value - mean) ** 2 for value in values) / n
    return mean, math.sqrt(variance)


def features_from_window(samples):
    rows = [tuple(row[:6]) for row in samples if len(row) >= 6]
    if not rows:
        return None
    accel_mags = []
    gyro_mags = []
    for ax, ay, az, gx, gy, gz in rows:
        accel_mags.append(math.sqrt(ax * ax + ay * ay + az * az) / LSB_PER_G)
        gyro_mags.append(math.sqrt(gx * gx + gy * gy + gz * gz) / LSB_PER_DPS)
    accel_mean, accel_std = _mean_std(accel_mags)
    gyro_mean, gyro_std = _mean_std(gyro_mags)
    axis_stds = tuple(_mean_std([row[i] for row in rows])[1] / (LSB_PER_G if i < 3 else LSB_PER_DPS) for i in range(6))
    # MicroPython rejects starred expressions in tuple displays.
    return (
        accel_mean, accel_std, min(accel_mags), max(accel_mags),
        gyro_mean, gyro_std,
    ) + axis_stds + (
        math.sqrt(sum(value * value for value in axis_stds[3:])),
        math.sqrt(sum(value * value for value in axis_stds[:3])),
    )


def _classify(features):
%(window_tree)s


def _landing(features):
%(landing_tree)s


def predict_features(features):
    if not features:
        return DEFAULT_ACTION
    action = _classify(features)
    return action if action in LABELS else DEFAULT_ACTION


def predict_window(samples):
    """Classify one second of raw IMU samples as still or throw."""
    return predict_features(features_from_window(samples))


def drop_probability(samples):
    """Score the second after a landing; higher means more likely on the floor."""
    features = features_from_window(samples)
    return _landing(features) if features else 0.0


class Tracker:
    """Feed (ticks_ms, (ax, ay, az, gx, gy, gz)) samples; read .state."""

    def __init__(self):
        self.state = DEFAULT_ACTION
        self.since = 0
        self.last_freefall = None
        self.judged = None  # last free fall whose landing was already scored
        self.drop_score = 0.0
        self._samples = []
        self._next_predict = 0

    def update(self, now, sample):
        samples = self._samples
        samples.append((now, sample))
        keep = max(WINDOW_MS, LANDING_DELAY_MS + LANDING_MS) + 100
        stale = 0
        while stale < len(samples) and now - samples[stale][0] > keep:
            stale += 1
        if stale:
            del samples[:stale]

        ax, ay, az, gx, gy, gz = sample[:6]
        g = math.sqrt(ax * ax + ay * ay + az * az) / LSB_PER_G
        if g < FREEFALL_G:
            self.last_freefall = now

        if self.state == "throw":
            landed = self.last_freefall
            if now - landed >= LANDING_DELAY_MS + LANDING_MS:
                start = landed + LANDING_DELAY_MS
                window = [s for t, s in samples if start <= t < start + LANDING_MS]
                self.drop_score = drop_probability(window)
                self.judged = landed
                self._enter("drop" if self.drop_score >= DROP_CONFIDENCE else "still", now)
            return self.state

        if self.state == "drop" and now - self.since < DROP_HOLD_MS:
            # Free fall right after a drop is the badge bouncing, not a throw.
            self.judged = self.last_freefall
            return self.state

        if self.state == "drop" and now - self.since >= DROP_HOLD_MS:
            dps = math.sqrt(gx * gx + gy * gy + gz * gz) / LSB_PER_DPS
            if abs(g - 1.0) > PICKUP_G or dps > PICKUP_DPS:
                self._enter("still", now)

        if now >= self._next_predict:
            self._next_predict = now + PREDICT_EVERY_MS
            recent = (
                self.last_freefall is not None
                and self.last_freefall != self.judged
                and now - self.last_freefall < WINDOW_MS
            )
            if recent and predict_window([s for t, s in samples if now - t < WINDOW_MS]) == "throw":
                self._enter("throw", now)
        return self.state

    def _enter(self, state, now):
        self.state = state
        self.since = now
'''


def write_model(window_model, output, landing_model=None, drop_confidence=DROP_CONFIDENCE):
    source = MODEL_TEMPLATE % {
        "feature_names": tuple(FEATURE_NAMES),
        "states": STATES,
        "default": DEFAULT_ACTION,
        "freefall_g": FREEFALL_G,
        "window_ms": WINDOW_MS,
        "landing_delay_ms": LANDING_DELAY_MS,
        "landing_ms": LANDING_MS,
        "drop_confidence": drop_confidence,
        "window_tree": window_source(window_model),
        "landing_tree": landing_source(landing_model),
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(source, encoding="utf-8")


def train(data_dir, output, drop_confidence=DROP_CONFIDENCE):
    (x, y, groups), (lx, ly, lgroups), sessions, window_counts = load_dataset(data_dir)
    model = window_tree()
    model.fit(x, y)
    landing_model = None
    if set(ly) == {"caught", "dropped"}:
        landing_model = landing_tree()
        landing_model.fit(lx, ly)
    write_model(model, output, landing_model, drop_confidence)
    report = {
        "samples": len(x),
        "labels": dict(Counter(y)),
        "sessions": sessions,
        "windows_per_label": window_counts,
        "states": list(STATES),
        "airborne_rule": {
            "labels": list(AIRBORNE_LABELS),
            "freefall_g": FREEFALL_G,
            "min_airborne_ms": MIN_AIRBORNE_MS,
            "otherwise": "still when no free fall; skipped when partial",
        },
        "max_depth": MAX_DEPTH,
        "actual_depth": model.get_depth(),
        "grouped_validation": grouped_validation(x, y, groups),
        "landing": {
            "landings": dict(Counter(ly)),
            "trained": landing_model is not None,
            "delay_ms": LANDING_DELAY_MS,
            "window_ms": LANDING_MS,
            "features": list(LANDING_FEATURE_NAMES),
            "max_depth": LANDING_DEPTH,
            "prior": LANDING_PRIOR,
            "drop_confidence": drop_confidence,
            "validation": landing_validation(lx, ly, lgroups, drop_confidence),
        },
        "model": str(output),
        "default_action": DEFAULT_ACTION,
    }
    report_path = output.with_name("report.json")
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data", type=Path, default=Path("data"))
    parser.add_argument("--output", type=Path, default=Path("dist/model.py"))
    parser.add_argument(
        "--drop-confidence",
        type=float,
        default=DROP_CONFIDENCE,
        help="landing score needed to report drop instead of a catch (default: %(default)s)",
    )
    args = parser.parse_args()
    report = train(args.data, args.output, args.drop_confidence)
    print(f"Trained {report['samples']} windows; depth {report['actual_depth']}.")
    validation = report["grouped_validation"]
    if validation["status"] == "available":
        print(f"Grouped validation accuracy: {validation['accuracy']:.3f}")
    else:
        print(f"Grouped validation unavailable: {validation['reason']}")
    landing = report["landing"]
    print(f"Landings: {landing['landings']}")
    lv = landing["validation"]
    if lv["status"] == "available":
        print(
            f"Landing validation ({lv['method']}): {lv['false_drops']}/{lv['caught_landings']} false drops, "
            f"{lv['missed_drops']}/{lv['dropped_landings']} missed drops at confidence "
            f"(majority: {lv['majority_false_drops']} false, {lv['majority_missed_drops']} missed)"
        )
    print(f"Model: {args.output}")
    print(f"Report: {args.output.with_name('report.json')}")


if __name__ == "__main__":
    main()
