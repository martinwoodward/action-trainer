# Action Trainer

Action Trainer records labeled accelerometer/gyroscope sessions from a
Universe badge over Bluetooth, trains a small decision-tree classifier, and
exports a self-contained MicroPython `model.py` for a badge app in
[`badger/home`](https://github.com/badger/home).

The badge firmware and runtime app remain in `badger/home`; this repository
owns the laptop-side recorder, training pipeline, and generated model
artifact. The generated model is intentionally portable: copy it into the
target badge app (for example `badge/apps/airtime/model.py`) and have that
app feed every six-axis IMU sample to `model.Tracker().update()`.

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
`unknown`. The trained model has exactly three states, `still`, `throw` and
`drop`; walking, handling and unknown examples all train `still`, the default.

A **mixed** session records still, held, thrown and dropped data in one go.
The recorder runs a shuffled script of PUT DOWN, HOLD, THROW and MISS (a throw left to land on
the floor; saved as `drop`) prompts
shown in the browser and on the badge; *Takes* sets the number of throws and of
drops. Flight prompts advance once free fall has been detected and the badge
has settled for 1.5 s. The session is saved to `data/mixed/`, and its JSON
sidecar lists each prompt segment (`label`, `start_ms`, `end_ms`,
`flight_ms`). Training labels airborne windows from the throw/drop prompt
active when free fall began, still windows that sit wholly inside a PUT DOWN
prompt (after a 1 s reaction delay) as `still`, and other grounded windows as
`unknown`.

A **landings** session focuses on catches versus misses: a shuffled script of
THROW (catch it) and MISS prompts only. Each prompt waits for the throw and
moves on 1.5 s after the landing, leaving time to pick the badge up. *Takes* sets the number of catches and of misses.
It is saved to `data/landings/` and trained exactly like a mixed session. Vary
the misses (height, surface, gentle landings that barely bounce): quiet misses
are the ones the drop model finds hardest to tell from a steady catch.

## Train and export

```powershell
python train.py --data data --output dist/model.py
```

Training windows are 1 second long with a 250 ms hop. Features are computed
from measured timestamps and six-axis IMU samples; the decision tree is
capped at depth 6. Grouped cross-validation keeps each recorded session wholly
inside one fold. The command writes a self-contained, MicroPython-compatible
`model.py` and a training report beside it. At least two sessions per class
are needed for grouped cross-validation; the model can still be fit with fewer
sessions, but the report will flag validation as unavailable.

Training has two stages:

1. **Window model (still/throw).** Throw and drop sessions include handling
   between takes, so only windows with at least 150 ms of free fall
   (|a| < 0.4 g) are labelled `throw` (airborne, whether thrown or dropped).
   Windows with no free fall are `still`, and windows that clip less than
   150 ms of a flight are skipped.
2. **Landing model (caught/dropped).** Free fall separated by less than 0.5 s
   (bounces, juggles) is one flight. The impact, the 0.5 s right after its
   last free fall, is scored with orientation-independent features: a floor
   impact jolts differently from a hand absorbing the catch. (Scoring later,
   once the badge has settled, separated catches from misses less well.) Flights
   from throw sessions or THROW prompts are `caught`, from drop sessions or
   MISS prompts `dropped`. Each leaf's score is `drops / (leaf samples + 2)`,
   which shrinks small leaves toward caught. The report includes
   leave-one-session-out landing validation.

The generated `Tracker` turns samples into a state at all times. It enters
`throw` when the window model sees free fall, and 0.5 s after the last free
fall it reports `drop` only if the landing score reaches the drop confidence;
otherwise it assumes the catch and returns to `still`. A drop is therefore
only possible after a throw. `drop` stays until the badge has shown it for
1.5 s and is picked up (or thrown again). Raise or lower the bar with:

```powershell
python train.py --drop-confidence 0.9
```

`Tracker().update(ticks_ms, (ax, ay, az, gx, gy, gz))` takes raw IMU samples
as streamed by the badge and returns `still`, `throw`, or `drop`.
`predict_window(samples)` (still/throw) and `drop_probability(samples)`
(landing score) are also exported.

The generated artifact is not automatically deployed. Copy the reviewed
`model.py` into the target app in `badger/home` and use that app's deployment
workflow. This keeps model generation separate from badge firmware changes.

## Kiosk badge (Airtime)

For a badge inside a plushy, this repository carries its own copy of the
Airtime app (`badge/apps/airtime/`) and a kiosk boot script (`badge/main.py`).
With the kiosk installed, every boot goes straight into Airtime, including a
bump of the reset switch: there is no startup animation and no menu. The app
has no exit; if it ever returns, the badge reboots into it. The recorder
reconnects over Bluetooth by itself once the badge advertises again. A
recording that is cut off by a reboot is still kept as a pending recording.

Deploy over USB serial (the port is auto-detected; close other serial users
first):

```powershell
python tools/deploy_badge.py kiosk    # app + dist/model.py, and /system/main.py
python tools/deploy_badge.py app      # update the app/model in /state only
python tools/deploy_badge.py restore  # stock boot (badge/main.orig.py)
```

`kiosk` and `restore` write `/system` by remounting its FAT read-write over
serial for the duration of the command. `main.py` boots
`/state/apps/airtime` and falls back to the copy in `/system/apps/airtime`.

Ways back in:

- Plug in over USB: `mpremote` interrupts the app as usual.
- Double-tap reset: the badge comes up as a `BADGER` USB drive. Eject it to
  reboot.

## BLE protocol

`protocol.py` mirrors the advertisement and GATT packet contract implemented
by `badge/apps/airtime/protocol.py` in `badger/home`. Keep the UUIDs, packet
field order, and struct formats synchronized; `tests/test_stream.py` checks
the recording packet format.

## Development checks

```powershell
python -m unittest discover -s tests
```
