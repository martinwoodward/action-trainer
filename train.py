"""Train and export a small badge-compatible decision tree.

Example: python train.py --data data --output dist/model.py
"""
import argparse
import csv
import json
import re
from collections import Counter, defaultdict
from pathlib import Path

from sklearn.metrics import accuracy_score, confusion_matrix
from sklearn.model_selection import GroupKFold
from sklearn.tree import DecisionTreeClassifier

import features
from features import AXIS_NAMES, extract_features

LABELS = ("drop", "still", "throw", "unknown", "walk")
IGNORE = "ignore"
# Free-form recordings: only tagged segments train; untagged time is ignored.
RECORDINGS = "recordings"
WINDOW_MS = 1000
HOP_MS = 250
MAX_DEPTH = 6
# Back-to-back walk predictions needed before the badge counts steps; cuts
# steps counted while waving or handling the badge.
WALK_CONFIRM = 2
SPLITS_FILE = "splits.json"
# Every Nth recording (in time order) is held out as the fixed test set.
TEST_EVERY = 4


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


def read_segments(path):
    """Annotated stretches from the session's JSON sidecar, if any."""
    sidecar = path.with_suffix(".json")
    if not sidecar.is_file():
        return []
    meta = json.loads(sidecar.read_text(encoding="utf-8"))
    segments = []
    for seg in meta.get("segments") or ():
        label = str(seg["label"]).lower()
        if label not in LABELS and label != IGNORE:
            raise ValueError(f"{sidecar}: unknown segment label {label!r}")
        segments.append((int(seg["start_ms"]), int(seg["end_ms"]), label))
    return segments


def window_label(start, end, label, segments):
    """Label for [start, end): the segment that wholly contains it, else the
    session label. Windows that straddle a segment edge are ambiguous (None)."""
    for seg_start, seg_end, seg_label in segments:
        if seg_start <= start and end <= seg_end:
            return None if seg_label == IGNORE else seg_label
        if start < seg_end and seg_start < end:
            return None
    return label


def window_session(samples, label, group, segments=(), raw=None):
    if not samples:
        return [], [], []
    x, y, groups = [], [], []
    start = samples[0][0]
    end = samples[-1][0]
    while start + WINDOW_MS <= end:
        target = window_label(start, start + WINDOW_MS, label, segments)
        window = [axes for t_ms, axes in samples if start <= t_ms < start + WINDOW_MS] if target else []
        if len(window) >= 2:
            x.append(extract_features(window))
            y.append(target)
            groups.append(group)
            if raw is not None:
                raw.append(window)
        start += HOP_MS
    return x, y, groups


def session_folders(data_dir):
    """(folder, default label) pairs. Legacy per-label folders treat untagged
    time as that label; the recordings folder ignores untagged time."""
    if not data_dir.is_dir():
        return []
    out = []
    for label_dir in sorted(path for path in data_dir.iterdir() if path.is_dir()):
        name = label_dir.name.lower()
        if name in LABELS:
            out.append((label_dir, name))
        elif name == RECORDINGS:
            out.append((label_dir, None))
    return out


def label_seconds(data_dir):
    """Seconds of training time per label, from the JSON sidecars."""
    totals = Counter({label: 0.0 for label in LABELS})
    for label_dir, default in session_folders(data_dir):
        for path in label_dir.glob("*.csv"):
            sidecar = path.with_suffix(".json")
            meta = json.loads(sidecar.read_text(encoding="utf-8")) if sidecar.is_file() else {}
            tagged = 0.0
            for start, end, label in read_segments(path):
                tagged += (end - start) / 1000
                if label != IGNORE:
                    totals[label] += (end - start) / 1000
            if default:
                totals[default] += max(0.0, float(meta.get("duration_s") or 0) - tagged)
    return {label: round(totals[label], 1) for label in LABELS}


def load_dataset(data_dir, raw=None):
    x, y, groups = [], [], []
    sessions_by_label = defaultdict(int)
    samples_by_label = Counter()
    for label_dir, label in session_folders(data_dir):
        for path in sorted(label_dir.glob("*.csv")):
            samples = read_session(path)
            group = str(path.parent.name + "/" + path.stem)
            sx, sy, sg = window_session(samples, label, group, read_segments(path), raw)
            if sx:
                x.extend(sx)
                y.extend(sy)
                groups.extend(sg)
                for seen in set(sy):
                    sessions_by_label[seen] += 1
                samples_by_label.update(sy)
    if not x:
        raise ValueError(f"No complete 1-second training windows found under {data_dir}")
    return x, y, groups, dict(sessions_by_label), dict(samples_by_label)


def assign_splits(data_dir, groups):
    """Persisted train/test role per recording. New recordings are assigned in
    time order and never move, so the test set stays a fixed yardstick."""
    path = Path(data_dir) / SPLITS_FILE
    splits = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    new = sorted(set(groups) - set(splits), key=lambda g: (g.rsplit("/", 1)[-1], g))
    for group in new:
        splits[group] = "test" if len(splits) % TEST_EVERY == TEST_EVERY - 1 else "train"
    if new:
        path.write_text(json.dumps(splits, indent=2, sort_keys=True), encoding="utf-8")
    return splits


def load_model_source(source):
    scope = {"__name__": "badge_model"}
    exec(compile(source, "model.py", "exec"), scope)
    return scope["predict_window"]


def score_model(source, windows, truths):
    """Run the exported model on raw test windows, as the badge would."""
    predict = load_model_source(source)
    truths = ["still" if t == "unknown" else t for t in truths]
    preds = [predict(w) for w in windows]
    recall = {}
    for label in ("walk", "still", "throw", "drop"):
        hits = [p == label for t, p in zip(truths, preds) if t == label]
        if hits:
            recall[label] = sum(hits) / len(hits)
    not_walk = [p != "walk" for t, p in zip(truths, preds) if t != "walk"]
    if not_walk:
        recall["not_walk"] = sum(not_walk) / len(not_walk)
    return {
        "windows": len(truths),
        "accuracy": sum(t == p for t, p in zip(truths, preds)) / len(truths),
        "recall": recall,
        "fake_steps": 1 - recall["not_walk"] if "not_walk" in recall else None,
        "missed_steps": 1 - recall["walk"] if "walk" in recall else None,
    }


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
        model = DecisionTreeClassifier(max_depth=MAX_DEPTH, random_state=42)
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


def tree_source(model):
    tree = model.tree_
    classes = [str(value) for value in model.classes_]

    def emit(node, depth):
        indent = "    " * depth
        feature = tree.feature[node]
        if feature < 0:
            cls = classes[int(tree.value[node][0].argmax())]
            return indent + "return " + repr(cls)
        lines = [
            f"{indent}if features[{feature}] <= {tree.threshold[node]:.17g}:",
            emit(tree.children_left[node], depth + 1),
            f"{indent}else:",
            emit(tree.children_right[node], depth + 1),
        ]
        return "\n".join(lines)

    return emit(0, 1)


def features_source():
    """features.py minus its docstring and import, for embedding in model.py."""
    text = Path(features.__file__).read_text(encoding="utf-8")
    return re.sub(r'^""".*?"""\s*import math\s*', "", text, count=1, flags=re.S).strip()


def model_source(model):
    return '''"""Generated by Action Trainer; do not edit by hand."""
import math

MODEL_VERSION = 3
LABELS = ("still", "walk", "throw", "drop", "unknown")
DEFAULT_ACTION = "still"
# Walk only counts once this many back-to-back calls (one per hop) agree.
WALK_CONFIRM = %d
_walk_streak = 0

%s


def features_from_window(samples):
    try:
        return extract_features(samples)
    except ValueError:
        return None


def _classify(features):
%s


def predict_features(features):
    if not features:
        return DEFAULT_ACTION
    action = _classify(features)
    return DEFAULT_ACTION if action == "unknown" else action


def predict_window(samples):
    """Classify six-axis raw IMU samples; unknown/empty windows default to Still.
    Call once per hop: a lone walk window between other actions reads as Still."""
    global _walk_streak
    action = predict_features(features_from_window(samples))
    if action != "walk":
        _walk_streak = 0
        return action
    _walk_streak += 1
    return action if _walk_streak >= WALK_CONFIRM else DEFAULT_ACTION
''' % (WALK_CONFIRM, features_source(), tree_source(model))


def write_model(model, output):
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(model_source(model), encoding="utf-8")


def train(data_dir, output, baseline=None):
    """Train on the train split and score on the fixed test split. `baseline`
    is an older model.py to score on the same test windows for comparison."""
    raw = []
    x, y, groups, sessions, window_counts = load_dataset(data_dir, raw)
    splits = assign_splits(data_dir, groups)
    is_test = [splits.get(g) == "test" for g in groups]
    train_idx = [i for i, t in enumerate(is_test) if not t] if not all(is_test) else list(range(len(y)))
    test_idx = [i for i, t in enumerate(is_test) if t] if any(not t for t in is_test) else []
    model = DecisionTreeClassifier(max_depth=MAX_DEPTH, random_state=42)
    model.fit([x[i] for i in train_idx], [y[i] for i in train_idx])
    validation = grouped_validation(x, y, groups)
    write_model(model, output)
    test = None
    if test_idx:
        windows = [raw[i] for i in test_idx]
        truths = [y[i] for i in test_idx]
        test = score_model(output.read_text(encoding="utf-8"), windows, truths)
        test["recordings"] = sorted({groups[i] for i in test_idx})
        if baseline is not None and Path(baseline).is_file():
            try:
                test["baseline"] = score_model(Path(baseline).read_text(encoding="utf-8"), windows, truths)
            except Exception as error:  # an old model that can't run scores as absent
                test["baseline_error"] = str(error)
    report = {
        "samples": len(x),
        "train_windows": len(train_idx),
        "test": test,
        "labels": dict(Counter(y)),
        "sessions": sessions,
        "windows_per_label": window_counts,
        "max_depth": MAX_DEPTH,
        "actual_depth": model.get_depth(),
        "grouped_validation": validation,
        "model": str(output),
        "default_action": "still",
        "unknown_prediction": "mapped to still",
    }
    report_path = output.with_name("report.json")
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data", type=Path, default=Path("data"))
    parser.add_argument("--output", type=Path, default=Path("dist/model.py"))
    args = parser.parse_args()
    report = train(args.data, args.output)
    print(f"Trained {report['train_windows']} windows; depth {report['actual_depth']}.")
    test = report["test"]
    if test:
        print(f"Test set ({len(test['recordings'])} recordings, {test['windows']} windows): "
              f"accuracy {test['accuracy']:.3f}")
        for label, value in test["recall"].items():
            print(f"  {label:9} {value:.0%}")
    validation = report["grouped_validation"]
    if validation["status"] == "available":
        print(f"Grouped validation accuracy: {validation['accuracy']:.3f}")
    else:
        print(f"Grouped validation unavailable: {validation['reason']}")
    print(f"Model: {args.output}")
    print(f"Report: {args.output.with_name('report.json')}")


if __name__ == "__main__":
    main()
