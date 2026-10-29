# Race: plushy air-time leaderboard

This folder is the Race fork of Action Trainer: badges inside plushies report
their air time over Wi-Fi to a leaderboard. It has its own recorder
(`race_rec.py`), a two-stage still/throw/drop trainer (`race_train.py`), the
Race kiosk badge app (`badge/apps/race/`), the leaderboard
(`leaderboard.py`) and its own copy of the wire formats (`protocol.py`).
Nothing here imports from the repository root, which holds the upstream
Airtime pedometer and gesture-training app, so upstream changes merge cleanly.

The generated model goes into the Race app as `model.py`; the app feeds every
six-axis IMU sample to `model.Tracker().update()`.

## Setup

Set up the virtual environment from the repository root (see the root
`README.md`), then run everything below from this folder:

```powershell
cd race
```

To start the Race leaderboard, the Action Trainer recorder and the Universe
Walker preview server, and open their pages, run from the repository root:

```powershell
.\start.ps1
```

To add an **Action Trainer** shortcut to the desktop and Start menu that runs
this launcher:

```powershell
.\start.ps1 -CreateShortcut
```

The launcher creates the root `.venv` if needed (Python 3.11 or newer must
be on PATH) and installs missing dependencies from the root `requirements.txt`.
It starts each server in its own working directory and a separate console window,
and reuses an already-running matching server. Leave the consoles open;
press Ctrl+C in each to stop it. The leaderboard is at `http://localhost:8766`
and Action Trainer is at `http://localhost:8765`, with guided recording and
BLE Train & deploy. This is the root `recorder.py`, not `race_rec.py`;
it records and trains Airtime models using the root `data/` and `dist/`.
Stop any Race recorder already using port 8765 before launching it.
Universe Walker's live badge dashboard (`dashboard/server.py`) runs from a
clone of [martinwoodward/universe-walker](https://github.com/martinwoodward/universe-walker)
(`main`) next to this repository, named `universe-walker` or
`martinwoodward_universe-walker` (or pass `-UniverseWalkerPath`/`$env:UNIVERSE_WALKER_PATH`),
at `http://127.0.0.1:8767/`; it uses port 8767 because the recorder has 8765.
Once all three are up, it opens the browser in this order: the
[badge simulator](https://badger.github.io/simulator/), Action Trainer,
the Universe Walker dashboard (`http://localhost:8767/dashboard/`) and the
leaderboard.

The launcher also turns on **Automatically hide the taskbar** (the same
Windows setting as in Settings > Personalization > Taskbar) just before it
opens the pages. Windows keeps the setting after the launcher exits; to turn it
back off, run:

```powershell
.\start.ps1 -ShowTaskbar
```

For Race-specific recording and training, use the commands below instead.

## Record

Start a BLE build of a badge app that streams IMU data (Race itself is
Wi-Fi only), then launch the local recorder:

```powershell
python race_rec.py
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
python race_train.py   # data/ -> dist/race_model.py and dist/report.json
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
python race_train.py --drop-confidence 0.9
```

`Tracker().update(ticks_ms, (ax, ay, az, gx, gy, gz))` takes raw IMU samples
as streamed by the badge and returns `still`, `throw`, or `drop`.
`predict_window(samples)` (still/throw) and `drop_probability(samples)`
(landing score) are also exported.

The generated model is not deployed automatically: `tools/deploy_race.py`
installs `dist/race_model.py` as the Race app's `model.py`.

## Kiosk badge (Race)

For a badge inside a plushy, this folder carries the
Race app (`badge/apps/race/`) and a kiosk boot script (`badge/main.py`).
With the kiosk installed, every boot goes straight into Race, including a
bump of the reset switch: there is no startup animation and no menu. The app
has no exit; if it ever returns, the badge reboots into it. Race reports
to the leaderboard over Wi-Fi and has no Bluetooth, so the recorder can't
record from it; use a BLE build of the app for recording sessions.

Deploy over USB serial (the port is auto-detected; close other serial users
first):

```powershell
python tools/deploy_race.py kiosk    # app + dist/race_model.py, and /system/main.py
python tools/deploy_race.py app      # update the app/model in /state only
python tools/deploy_race.py restore  # stock boot (badge/main.orig.py)
```

`app` and `kiosk` also send the Wi-Fi settings, which are stored on the badge
in `/state/airtime_net.json`. Put them in `wifi.json` (gitignored), copied
from `wifi.example.json`:

```json
{ "ssid": "MyNetwork", "password": "...", "server": "auto" }
```

`AIRTIME_WIFI_SSID`/`AIRTIME_WIFI_PASSWORD` and `--server` override the
file. `--wifi-ssid` and `--wifi-password` work too but land in your shell
history.
`--server auto` uses this computer's current IP; `--server none` makes the
badge broadcast until the leaderboard answers. Without stored credentials, the
badge uses `WIFI_SSID` and `WIFI_PASSWORD` from `/system/secrets.py`. The tool
never prints the password; don't commit it.

`kiosk` and `restore` write `/system` by remounting its FAT read-write over
serial for the duration of the command. `main.py` boots
`/state/apps/race` and falls back to the copy in `/system/apps/race`.

Ways back in:

- Plug in over USB: `mpremote` interrupts the app as usual.
- Double-tap reset: the badge comes up as a `BADGER` USB drive. Eject it to
  reboot.

## State-time leaderboard

`leaderboard.py` follows any number of badges over Wi-Fi: each badge sends a
UDP status datagram to port 47217 every 250 ms, and at once when its state
changes. It shows each badge's **air time** (time in the `throw` state) since it was reset,
and a **total air time** bar chart with one horizontal bar per badge, in the
badge's colour, with its name on the left and its air time since reset at the
right-hand end. A badge joins the chart once it has air time since the last reset, so
Reset all clears the chart. Bars are sorted from most to least air time, top down; when a
badge overtakes another, the bars slide past each other (600 ms). Offline badges stay on the
chart, faded; a badge not heard for 60 s falls off the bottom of the chart and its card goes,
and it rises back in if it's heard again. Each badge has its own colour from the GitHub brand palette
(`PALETTE` in `protocol.py`): the leaderboard gives each new badge the next
free colour, keeps it in `leaderboard_state.json` and sends it to the badge in
its acks, so the bar, the card and the badge's own screen match and no two
badges share a colour. With more badges than colours, colours are reused.

Each badge's picture and sounds come from `web/plushies/`: a badge whose name
contains a folder's name (for example Duckie → `duck/`, Copilot 2 →
`copilot/`) uses that folder's `icon`, `throw` and `drop` files, and
`default/` fills in anything missing. The picture sits at the right-hand end
of its bar and next to its name on its card. See
[`web/plushies/README.md`](web/plushies/README.md) for the conventions, the
recommended audio format and the credits.

The badge's own screen matches its card: a white card with the same colour
swatch, picture (24×24 PNGs in `badge/apps/race/assets/`, copied by
`tools/deploy_race.py`), name and air time, an **IN THE AIR** pill in the
badge's colour while it flies, **DROPPED** in red, its battery level at the
top right (`badge.battery_level()`, read every 2 s; red at 20% or below;
while charging the percentage is hidden and the icon fills up on a loop), and its throw/drop counts and Wi-Fi address at the bottom.
Names too long to fit are shortened with `..`. Find blinks the screen in the badge's colour.
Badges named after other animals show no picture on the badge.
Turn on sound with the speaker button in the header (it starts muted on every
load) and the leaderboard's computer plays a plushie's `throw` sound once each time it
leaves the ground: a rubber-duck squeak for Duckie, a chitter for
Squirrel, a sparkly arpeggio for Copilot, bubble bloops for Mona, and a
"wheee" for anyone else. Plushies in the air together are heard together. A
flight too short to be reported still plays its sound once. When a badge
reports a drop (its drop counter goes up), the page plays its `drop` sound
once, by default a short "UGF!". Badges sharing a default sound are pitched a
little apart. Click a badge's picture on its card to hear its throw sound, or
Shift-click it to hear its drop. Browsers only allow sound after a click on
the page.

**Ending a race:** press the space bar on the leaderboard page (the page
swallows it, so it doesn't scroll or press a button). "Race ending in.." and a
10-second countdown appear over the chart in an arcade font. At zero the air
time counters stop, "X Won!" names the badge with the most air time with a
burst of confetti, and a 2-1-3 podium with each plushie's picture stands in
front of fireworks until you press **Reset all**. Escape cancels a countdown.
The arcade font (Press Start 2P) comes from Google Fonts; offline, the page
falls back to a monospace font.

**Simulated race (test mode):** after **Reset all**, press **S** to load one
simulated badge for every plushie in `web/plushies/` (except `default`), then
**Enter** (or keypad Enter) to set them racing with random, fast flights and
the odd drop. They race alongside any real badges, so press space as usual to
end the race; the simulation stops when the winner is declared, and **Reset
all** removes the simulated badges. This runs only in the page: the server and
real badges never see it.

The page uses the light theme of [badger.github.io](https://badger.github.io/)
(colours, fonts and card styles from its `src/styles/globals.css`). To add more
plushies, add folders to `web/plushies/`. The server saves the known badges and
the page-reset baselines to `leaderboard_state.json`, so neither a page reload
nor a server restart empties the chart:

```powershell
python leaderboard.py              # http://localhost:8766
```

- **Counting is done on the badge.** The Race app in this repository adds up
  milliseconds per state while it runs and sends the totals in every status.
  The totals stay exact while the laptop is unreachable.
- **Network:** the badges and this computer must be on the same 2.4 GHz
  network, and it must allow client-to-client traffic. Many venue and guest
  networks isolate clients; then nothing arrives, and a phone hotspot or a
  travel router is the fix. A badge sends to its configured server, otherwise
  broadcasts, and switches to unicast once the leaderboard acks it. Every 30 s
  it pings its server; after three unanswered pings (about 3 s) it goes back to
  broadcast auto-discovery, even if the server was configured. On macOS,
  allow incoming connections for Python if the firewall asks.
- **Rename and Find:** each card has **Rename**, which saves the new name on
  the badge, and **Find**, which flashes the badge's screen. Both are enabled
  while the badge is online.
- **Resets:** badges never reset their counters. Each badge saves its lifetime
  totals to `/state` on every landing and every 60 s, so a reboot carries on
  from where it was; a power cut loses at most 60 s of still time. Times are
  sent as 64-bit milliseconds and counts as 32-bit, so they don't wrap.
  **Reset** on the page (per badge or **Reset all**) records the badge's latest
  totals as an offset in `leaderboard_state.json`, and the times shown are the
  lifetime totals minus that offset. Offsets survive badge reboots and
  leaderboard restarts. Each ack tells the badge its air-time offset (and its colour), so the
  badge screen shows air time since the last reset with its lifetime total
  below. Its footer shows the throw and drop counts, the badge's IP (`Me`), and the
  leaderboard's IP (`Srv`, or `Srv broadcast` while it searches) with the
  seconds since the leaderboard last answered.
- **Out of range:** a badge that goes quiet greys out after 5 s. When it
  returns, its bar catches up with what the badge counted meanwhile.
- **Latency:** the server pushes each status to the page as it arrives.
  Measured with Copilot on venue Wi-Fi: a status every 250 ms (p50 255 ms,
  p90 360–470 ms), 5–15% of datagrams lost on a busy 2.4 GHz channel, and a
  ping round trip of 35–55 ms p50 (p90 80–120 ms). A state change is sent at
  once and repeated 60 ms later, so a take-off usually reaches the page in
  well under 100 ms; in a 20 s test, all 20 state changes arrived. A badge
  is back online about 7 s after a reset. While
  a badge is in the air, the page grows its bar and air time live, for up
  to 3 s past the last report. To measure packet gaps, loss and ping round
  trips, stop the leaderboard and run `python tools/udp_latency.py --seconds 60`.
  The old Bluetooth builds updated about every 0.3–0.5 s (p50), with gaps up to
  4.5 s; `tools/adv_latency.py` still measures those.

## Protocol

The Wi-Fi status, ack, command and reply datagrams are the UDP section of
`protocol.py`: a `<2sBBI` header (magic `AT`, version 2, type, badge id),
then a fixed status body and the name (up to 16 bytes). Commands are `N`
(name), `I` (identify) and `P` (ping, echoed back).

The BLE part of `protocol.py` is a fork of the advertisement and GATT packet
contract in the repository root's `protocol.py`, used by `race_rec.py` and
`listen.py`. It's kept separate so upstream changes don't conflict; keep the
UUIDs, field order and struct formats compatible with the BLE build you
record from.

The state totals are not in the 31-byte manufacturer advert, which is full.
They travel in the scan response, as service data under the 16-bit UUID
`0xA17E` (`<BIII>`: boot, still_ms, throw_ms, drop_ms). There they fit next to
the name, which the scan response cuts to 12 characters.

## Development checks

```powershell
python -m unittest discover -s tests
```
