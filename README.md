# Action Trainer

Action Trainer records labeled accelerometer/gyroscope sessions from a
Universe badge over Bluetooth, trains a small decision-tree classifier, and
exports a self-contained MicroPython `model.py` for a badge app in
[`badger/home`](https://github.com/badger/home).

The badge firmware and runtime app remain in `badger/home`; this repository
owns the laptop-side recorder, training pipeline, and generated model
artifact. The generated model is intentionally portable: copy it into the
target badge app (for example `badge/apps/pedometer/model.py`) and have that
app call `predict_window()` with a one-second window of six-axis IMU samples.

## Setup

Python 3.11 or newer is recommended.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

Start the badge's Airtime/Pedometer app, then launch the local recorder:

```powershell
python recorder.py
```

Open `http://localhost:8765` if it does not open automatically. Select the
badge, record sessions in the intended physical configuration (including the
plushy, if that is how the badge will be used), and save each labeled session.
Each session is stored under `data/<label>/` as a CSV plus a JSON metadata
sidecar. `data/` is intentionally ignored by Git; review and curate training
data explicitly before sharing or committing it.

The recorder captures five labels: `throw`, `drop`, `still`, `walk`, and
`unknown`. Unknown/handling examples are useful training data, but the
generated model maps an `unknown` prediction to `still` as the safe default
detected action.

After you stop a recording, the review panel shows the whole session. Drag
across the chart to select a stretch and tag it (buttons or keys 1–6), for
example `unknown` for the times you lifted the badge up and down during a
`walk` session, or `ignore` for set-up time. Tags are saved as `segments`
(`start_ms`, `end_ms`, `label`) in the JSON sidecar. Untagged time keeps the
session's label.

## Train and export

```powershell
python train.py --data data --output dist/model.py
```

Training windows are 1 second long with a 250 ms hop. A window wholly inside
a tagged segment takes that segment's label; windows that cross a segment edge
or fall inside an `ignore` segment are skipped. Features are computed
from measured timestamps and six-axis IMU samples; the decision tree is
capped at depth 6. Grouped cross-validation keeps each recorded session wholly
inside one fold. The command writes a self-contained, MicroPython-compatible
`model.py` and a training report beside it. At least two sessions per class
are needed for grouped cross-validation; the model can still be fit with fewer
sessions, but the report will flag validation as unavailable.

`predict_window(samples)` accepts an iterable of six-element tuples
`(ax, ay, az, gx, gy, gz)` in the same raw IMU units streamed by the badge. It
returns one of `still`, `walk`, `throw`, or `drop`; a learned `unknown` leaf
and empty windows are mapped to `still`.

The generated artifact is not automatically deployed. Copy the reviewed
`model.py` into the target app in `badger/home` and use that app's deployment
workflow. This keeps model generation separate from badge firmware changes.

## BLE protocol

`protocol.py` mirrors the advertisement and GATT packet contract implemented
by `badge/apps/airtime/protocol.py` in `badger/home`. Keep the UUIDs, packet
field order, and struct formats synchronized; `tests/test_stream.py` checks
the recording packet format.

## Development checks

```powershell
python -m unittest discover -s tests
```
