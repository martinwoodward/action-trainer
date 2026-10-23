# Action Trainer

Action Trainer records labeled accelerometer/gyroscope sessions from a
Universe badge over Bluetooth, trains a small decision-tree classifier, and
exports a self-contained MicroPython `model.py`.

This repository holds both sides: the laptop recorder and training pipeline,
and the **Airtime** badge app (`badge/apps/airtime/`) that streams IMU data to
the recorder, accepts models over BLE, and runs them. The badge firmware and
the other badge apps live in [`badger/home`](https://github.com/badger/home).
The generated model is portable: any badge app can call `predict_window()`
with a one-second window of six-axis IMU samples.

## Setup

Python 3.11 or newer is recommended.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
```

## Install the badge app

Plug the badge in over USB-C, close anything else using its serial port
(Thonny, Web Serial pages), then:

```powershell
python tools/deploy_badge.py --autostart
```

This copies `badge/apps/airtime/` to `/state/apps/airtime` on the badge and
finds the port automatically (pass `--port COM13` if several badges are
attached). Press the badge's **reset** button to start it.

- `--autostart` launches Airtime after the boot animation on power-on, reset
  and wake. It patches `/system/main.py` once (the stock file is saved to
  `/state/main.orig.py`); **HOME** still exits to the menu and the badge still
  sleeps normally. `--no-autostart` goes back to booting into the menu.
- `--name martin` sets the name the badge advertises.
- `--model dist/model.py` installs a model over USB. The recorder normally
  sends it over BLE instead.
- `--run` starts the app over serial straight away, printing its output.

To install it for good (it then shows in the menu), put the badge in USB
mass-storage mode and copy `badge/apps/airtime` to `system/apps/airtime`.

Start Airtime on the badge, then launch the local recorder:

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

To ship a model with another app, copy the reviewed `model.py` into that app
in `badger/home` and use its deployment workflow.

## BLE protocol

`protocol.py` defines the advertisement and GATT packet contract. The badge app
has an identical copy at `badge/apps/airtime/protocol.py` (the badge can only
import files from the app's folder); `tests/test_badge_tools.py` fails if the
two differ, and `tests/test_stream.py` checks the recording packet format.

The recorder pings a connected badge every 2 s and reconnects automatically,
backing off from 2 s to 15 s between failed attempts. Connection events are
logged with timestamps to `data/recorder.log`. The badge's CYW43 BLE stack can
wedge (centrals connect but see no services), so the Airtime app restarts
its BLE stack after two links that never send a command, or after 2 minutes
without a working link.

## Development checks

```powershell
python -m unittest discover -s tests
```

The badge app's detector and protocol are replayed on the laptop by
`tests/test_detect.py`. `python tools/make_icon.py` regenerates the app's
24×24 `icon.png` (needs Pillow).

## Race fork

[`race/`](race/README.md) is a separate fork of this tool for an air-time
leaderboard: badges inside plushies report over Wi-Fi to `race/leaderboard.py`.
It has its own recorder, trainer, protocol copy and kiosk badge app, and
imports nothing from this folder. See [`race/README.md`](race/README.md).
