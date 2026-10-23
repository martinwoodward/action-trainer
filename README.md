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
has no exit; if it ever returns, the badge reboots into it. Airtime reports
to the dashboard over Wi-Fi and has no Bluetooth, so the recorder can't
record from it; use a BLE build of the app for recording sessions.

Deploy over USB serial (the port is auto-detected; close other serial users
first):

```powershell
python tools/deploy_badge.py kiosk    # app + dist/model.py, and /system/main.py
python tools/deploy_badge.py app      # update the app/model in /state only
python tools/deploy_badge.py restore  # stock boot (badge/main.orig.py)
```

`app` and `kiosk` also take the Wi-Fi settings, which are stored on the badge
in `/state/airtime_net.json`:

```powershell
$env:AIRTIME_WIFI_SSID = "..."; $env:AIRTIME_WIFI_PASSWORD = "..."
python tools/deploy_badge.py kiosk --server auto
```

`--wifi-ssid` and `--wifi-password` work too but land in your shell history.
`--server auto` uses this computer's current IP; `--server none` makes the
badge broadcast until the dashboard answers. Without stored credentials, the
badge uses `WIFI_SSID` and `WIFI_PASSWORD` from `/system/secrets.py`. The tool
never prints the password; don't commit it.

`kiosk` and `restore` write `/system` by remounting its FAT read-write over
serial for the duration of the command. `main.py` boots
`/state/apps/airtime` and falls back to the copy in `/system/apps/airtime`.

Ways back in:

- Plug in over USB: `mpremote` interrupts the app as usual.
- Double-tap reset: the badge comes up as a `BADGER` USB drive. Eject it to
  reboot.

## State-time dashboard

`dashboard.py` follows any number of badges over Wi-Fi: each badge sends a
UDP status datagram to port 47217 every 250 ms, and at once when its state
changes. It shows each badge's **air time** (time in the `throw` state) since it was reset,
and graphs every badge's cumulative air time on a chart that scrolls with the
clock. You can choose a 1 min, 5 min, 15 min or 1 h window. A badge named
after an animal or a mascot (for example Duckie, Squirrel or Copilot) shows
that picture at the leading end of its line, and next to its name on its card.
Duckie is drawn as a yellow rubber duck; Copilot and Mona use the Copilot and
Octocat marks from Primer Octicons (MIT); Squirrel is the Twemoji squirrel
(CC-BY 4.0, [jdecked/twemoji](https://github.com/jdecked/twemoji)); the other
animals use emoji. Each badge keeps one colour on the page: Copilot blue,
Duckie orange, Mona purple, Squirrel pink, and for any other badge a colour
picked from its id.

The badge's own screen matches its card: a white card with the same colour
swatch, picture (24×24 PNGs in `badge/apps/airtime/assets/`, copied by
`tools/deploy_badge.py`), name and air time, an **IN THE AIR** pill in the
badge's colour while it flies, **DROPPED** in red, its battery level at the
top right (`badge.battery_level()`, read every 2 s; `+` while charging, red at
20% or below), and its throw/drop counts and Wi-Fi address at the bottom.
Names too long to fit are shortened with `..`. Find blinks the screen in the badge's colour.
Badges named after other animals show no picture on the badge. Keep `PALETTE`
and the namesake colours in step in `web/dashboard.html` and the app.
Turn on **Sound** in the header and the dashboard's computer plays a sound of
delight for as long as a plushie is in the air: a rubber-duck squeak for Duckie, a
chitter for Squirrel, a sparkly arpeggio for Copilot, bubble bloops for Mona,
and a "wheee" for anyone else. Each sound repeats until its plushie lands, and
plushies in the air together are heard together. A flight too short to be
reported still plays its sound once. When a badge reports a drop (its drop
counter goes up), the page plays one short "UGF!" (a thud, a grunt and a
breath), pitched a little differently per plushie; it plays once per drop,
however long the badge shows DROPPED. Click a badge's picture on its card to
hear its sound, or Shift-click it to hear its UGF. The sounds are synthesised in the page with Web Audio; browsers only
allow sound after a click on the page.

The page uses the light theme of [badger.github.io](https://badger.github.io/)
(colours, fonts and card styles from its `src/styles/globals.css`). To add more, edit
`NAMESAKES` in `web/dashboard.html`. Points are only
dropped from the chart once they are more than an hour old. The server saves
the history, the known badges and the page-reset baselines to
`dashboard_state.json`, so neither a page reload nor a server restart empties
the chart:

```powershell
python dashboard.py              # http://localhost:8766
```

- **Counting is done on the badge.** The Airtime app in this repository adds up
  milliseconds per state while it runs and sends the totals in every status.
  The totals stay exact while the laptop is unreachable.
- **Network:** the badges and this computer must be on the same 2.4 GHz
  network, and it must allow client-to-client traffic. Many venue and guest
  networks isolate clients; then nothing arrives, and a phone hotspot or a
  travel router is the fix. A badge sends to its configured server, otherwise
  broadcasts, and switches to unicast once the dashboard acks it. On macOS,
  allow incoming connections for Python if the firewall asks.
- **Rename and Find:** each card has **Rename**, which saves the new name on
  the badge, and **Find**, which flashes the badge's screen. Both are enabled
  while the badge is online.
- **Resets:** a badge reboot zeroes its counters. **Reset** on the page (per
  badge or **Reset all**) stores a baseline in `dashboard_state.json`, and the
  times shown are counted from that baseline. A restart of the dashboard keeps
  the baselines. The next reboot of that badge clears its baseline. **Reset
  all** also clears the chart history, on the server and in every open page.
- **Out of range:** a badge that goes quiet greys out after 5 s. When it
  returns, its line continues from where it left off. Because the badge kept
  counting, the jump across the gap is drawn as a dashed straight line.
- **Latency:** the server pushes each status to the page as it arrives.
  Measured with Copilot on venue Wi-Fi: a status every 250 ms (p50 255 ms,
  p90 360–470 ms), 5–15% of datagrams lost on a busy 2.4 GHz channel, and a
  ping round trip of 35–55 ms p50 (p90 80–120 ms). A state change is sent at
  once and repeated 60 ms later, so a take-off usually reaches the page in
  well under 100 ms; in a 20 s test, all 20 state changes arrived. A badge
  is back online about 7 s after a reset. While
  a badge is in the air, the page extends its line and air time live, for up
  to 3 s past the last report. To measure packet gaps, loss and ping round
  trips, stop the dashboard and run `python tools/udp_latency.py --seconds 60`.
  The old Bluetooth builds updated about every 0.3–0.5 s (p50), with gaps up to
  4.5 s; `tools/adv_latency.py` still measures those.

## Protocol

The Wi-Fi status, ack, command and reply datagrams are the UDP section of
`protocol.py`: a `<2sBBI` header (magic `AT`, version 2, type, badge id),
then a fixed status body and the name (up to 16 bytes). Commands are `N`
(name), `I` (identify) and `P` (ping, echoed back).

The BLE part of `protocol.py` mirrors the advertisement and GATT packet contract implemented
by `badge/apps/airtime/protocol.py` in `badger/home`. Keep the UUIDs, packet
field order, and struct formats synchronized; `tests/test_stream.py` checks
the recording packet format.

The state totals are not in the 31-byte manufacturer advert, which is full.
They travel in the scan response, as service data under the 16-bit UUID
`0xA17E` (`<BIII>`: boot, still_ms, throw_ms, drop_ms). There they fit next to
the name, which the scan response cuts to 12 characters. Only this
repository's copy of the protocol has the stats so far; `badger/home` doesn't
yet.

## Development checks

```powershell
python -m unittest discover -s tests
```
