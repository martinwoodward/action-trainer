"""Train and export a small badge-compatible decision tree.

Example: python train.py --data data --output dist/model.py
"""
import argparse
import csv
import json
from collections import Counter, defaultdict
from pathlib import Path

from sklearn.metrics import accuracy_score, confusion_matrix
from sklearn.model_selection import GroupKFold
from sklearn.tree import DecisionTreeClassifier

from features import AXIS_NAMES, FEATURE_NAMES, extract_features

LABELS = ("drop", "still", "throw", "unknown", "walk")
IGNORE = "ignore"
# Free-form recordings: only tagged segments train; untagged time is ignored.
RECORDINGS = "recordings"
WINDOW_MS = 1000
HOP_MS = 250
MAX_DEPTH = 6


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


def window_session(samples, label, group, segments=()):
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


def load_dataset(data_dir):
    x, y, groups = [], [], []
    sessions_by_label = defaultdict(int)
    samples_by_label = Counter()
    for label_dir, label in session_folders(data_dir):
        for path in sorted(label_dir.glob("*.csv")):
            samples = read_session(path)
            group = str(path.parent.name + "/" + path.stem)
            sx, sy, sg = window_session(samples, label, group, read_segments(path))
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


def write_model(model, output):
    source = '''"""Generated by Action Trainer; do not edit by hand."""
import math

MODEL_VERSION = 1
FEATURE_NAMES = %r
LABELS = ("still", "walk", "throw", "drop", "unknown")
DEFAULT_ACTION = "still"
LSB_PER_G = 16384.0
LSB_PER_DPS = 70.0


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
    axis_stds = [_mean_std([row[i] for row in rows])[1] for i in range(6)]
    return (
        accel_mean, accel_std, min(accel_mags), max(accel_mags),
        gyro_mean, gyro_std,
    ) + tuple(axis_stds[i] / (LSB_PER_G if i < 3 else LSB_PER_DPS) for i in range(6))


def _classify(features):
%s


def predict_features(features):
    if not features:
        return DEFAULT_ACTION
    action = _classify(features)
    return DEFAULT_ACTION if action == "unknown" else action


def predict_window(samples):
    """Classify six-axis raw IMU samples; unknown/empty windows default to Still."""
    features = features_from_window(samples)
    return predict_features(features)
''' % (tuple(FEATURE_NAMES), tree_source(model))
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(source, encoding="utf-8")


def train(data_dir, output):
    x, y, groups, sessions, window_counts = load_dataset(data_dir)
    model = DecisionTreeClassifier(max_depth=MAX_DEPTH, random_state=42)
    model.fit(x, y)
    validation = grouped_validation(x, y, groups)
    write_model(model, output)
    report = {
        "samples": len(x),
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
    print(f"Trained {report['samples']} windows; depth {report['actual_depth']}.")
    validation = report["grouped_validation"]
    if validation["status"] == "available":
        print(f"Grouped validation accuracy: {validation['accuracy']:.3f}")
    else:
        print(f"Grouped validation unavailable: {validation['reason']}")
    print(f"Model: {args.output}")
    print(f"Report: {args.output.with_name('report.json')}")


if __name__ == "__main__":
    main()
