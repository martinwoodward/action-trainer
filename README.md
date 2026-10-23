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

Open `http://localhost:8765` if it does not open automatically. Connect the
badge, press **Record**, and move however you like (walk, throw, drop, hold it
still, lift it up and down), in the intended physical configuration
(including the plushy, if that is how the badge will be used). Press **Stop**
when you're done.

For a hands-off run, pick **Walk**, **Throw** or **Drop** instead. The recorder
speaks timed prompts ("Start walking", "Stop. Fidget with it", "Throw it"),
shows them on screen and on the badge, and tags each
stretch from the prompt that was showing. It skips the first second after each
prompt while you react. The session saves itself when the run finishes;
**Cancel** throws it away.

With **Free record**, the review panel shows the whole recording. Drag across the chart to
select a stretch and tag it `walk`, `throw`, `drop`, `still`, or `other`
(buttons or keys 1–5). `other` is for lifting and handling and is stored as
the `unknown` label; the generated model maps an `unknown` prediction to
`still` as the safe default. Only tagged time is used for training.

Sessions are saved under `data/recordings/` as a CSV plus a JSON sidecar, with
tags stored as `segments` (`start_ms`, `end_ms`, `label`). Older sessions in
per-label folders (`data/<label>/`) still work: their untagged time counts as
the folder's label. `data/` is intentionally ignored by Git; review and curate training
data explicitly before sharing or committing it.

## Train and export

```powershell
python train.py --data data --output dist/model.py
```

Training windows are 1 second long with a 250 ms hop. A window wholly inside
a tagged segment takes that segment's label; windows that cross a segment edge,
fall inside an `ignore` segment, or sit in untagged time of a
`data/recordings/` session are skipped. Features come from the six-axis IMU
samples in each window: magnitude and per-axis spread, plus rhythm features
that separate steps from other movement (autocorrelation at step-like lags,
peak count and regularity, motion along vs. across gravity, and jerk). The
decision tree is capped at depth 6.

Every fourth recording, in time order, is held back as a fixed test set and
never trained on. Roles are stored in `data/splits.json` and don't change once
assigned, so test scores are comparable from one training run to the next. The
exported `model.py` is run on the test windows exactly as the badge would run
it, and `dist/report.json` records overall accuracy plus how often it spots
walking and ignores other movement. Grouped cross-validation over all
recordings is also reported for reference.

`model.py` embeds `features.py` directly, so the badge and the trainer always
compute the same features. Keep `features.py` plain MicroPython (only `math`).

`predict_window(samples)` accepts an iterable of six-element tuples
`(ax, ay, az, gx, gy, gz)` in the same raw IMU units streamed by the badge. It
returns one of `still`, `walk`, `throw`, or `drop`; a learned `unknown` leaf
and empty windows are mapped to `still`. Call it once per hop: it only reports
`walk` after `WALK_CONFIRM` (2) back-to-back walk windows, which keeps waving
and handling the badge from being counted as steps.

In the recorder, **Train & deploy** trains, scores the new model and the one
already on the badge against the same test recordings, and only sends the new
one if it scores at least as well. Otherwise it keeps the current model and
offers **Deploy anyway**. The deployed model and its scorecard are kept in
`dist/deployed_model.py` and `dist/deployed.json`.

To ship a model with an app, copy the reviewed `model.py` into the target app
in `badger/home` and use that app's deployment workflow.

## BLE protocol

`protocol.py` mirrors the advertisement and GATT packet contract implemented
by `badge/apps/airtime/protocol.py` in `badger/home`. Keep the UUIDs, packet
field order, and struct formats synchronized; `tests/test_stream.py` checks
the recording packet format.

The recorder pings a connected badge every 2 s and reconnects automatically,
backing off from 2 s to 15 s between failed attempts. Connection events are
logged with timestamps to `data/recorder.log`. The badge's CYW43 BLE stack can
wedge (centrals connect but see no services), so the pedometer app restarts
its BLE stack after two links that never send a command, or after 2 minutes
without a working link.

## Development checks

```powershell
python -m unittest discover -s tests
```
