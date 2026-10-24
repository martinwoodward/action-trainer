# Agent guide: Race

`race/` is a logical fork of Action Trainer, focused on an air-time
**leaderboard for a race** between plushies. The repository root is upstream's
Airtime pedometer and gesture-training app. **Keep Race work inside `race/`**,
don't import from or edit root files for it, and run commands from this folder.

Race has its own recorder (`race_rec.py`), trainer (`race_train.py`,
`race_features.py`), wire formats (`protocol.py`), leaderboard
(`leaderboard.py`) and the kiosk badge app for the plushies:
`badge/apps/race/` (Race without an exit) and `badge/main.py` (boot straight
into Race). Deploy both with `tools/deploy_race.py` (`kiosk`, `app` or
`restore`); `badge/main.orig.py` is the stock boot script.

## Before you start

- Read `../docs/badge/AGENTS.md` before writing or changing badge app code.
- Read `../docs/badge/USB_SERIAL.md` before connecting to a badge or changing its
  filesystem.
- Read `../docs/badge/HARDWARE.md` before using raw GPIO, I2C, ADC, IR, power,
  wireless, display, touch, switch, or interrupt hardware.
- These are copies of the docs in `badger/home` (`badge/AGENTS.md`,
  `hardware/USB_SERIAL.md`, `hardware/README.md`). If they disagree,
  `badger/home` wins; refresh the copies.

## Repository layout

- `race_rec.py` with `web/race_rec.html`: a bleak + aiohttp recorder UI at
  http://localhost:8765. Sessions are saved to `data/<label>/*.csv` + `.json`.
  A guided `mixed` session prompts PUT DOWN/HOLD/THROW/DROP and saves prompt
  `segments` in its JSON sidecar; `race_train.py` labels its windows from them.
  A guided `landings` session is the same but only THROW/MISS, to
  collect catch-versus-miss landings.
- `listen.py`: console listener for badge advertisements.
- `leaderboard.py` with `web/leaderboard.html`: a multi-badge page at
  http://localhost:8766. Badges send UDP status datagrams over Wi-Fi to port
  47217. The page shows air time (time in `throw`) since the last reset, with
  a horizontal bar chart of each badge's air time, sorted largest to smallest
  from the top, with animated swaps. The badge counts the
  ms per state and sends lifetime totals (saved in `/state`, never reset;
  64-bit ms, 32-bit counts) in each status. The server acks badges
  (so they switch from broadcast to unicast) and sends commands: rename,
  identify and ping. It also acks each badge `PKT_PING` at once; a badge pings
  every 30 s and broadcasts again after three unanswered pings.
  Acks carry the badge's air-time offset so its screen can show since-reset
  air time. `leaderboard_state.json` holds the page-reset offsets
  (lifetime totals at the reset; reboots don't clear them), and the known badges. `tools/udp_latency.py` measures packet gaps and ping round trips.
- The race app in this repo has **no BLE**: it talks Wi-Fi/UDP only, so
  `race_rec.py` and `listen.py` need a BLE build for recording. Its Wi-Fi
  settings live in `/state/airtime_net.json` (`ssid`, `password`, `server`),
  written by `tools/deploy_race.py --wifi-ssid/--wifi-password/--server`
  (or `AIRTIME_WIFI_*` env vars); it falls back to `WIFI_*` in
  `/system/secrets.py`. Never commit Wi-Fi credentials.
- The race screen mirrors the badge's leaderboard card: keep `PALETTE` and
  the namesake → colour/picture table identical in
  `badge/apps/race/__init__.py` and `web/leaderboard.html`. Pictures are
  24×24 PNGs in `badge/apps/race/assets/`; the deploy tool copies them.
- `protocol.py`: the advertisement, GATT and UDP formats; the deploy tool
  copies it into the Race app. It's a fork of the root `protocol.py`, kept
  separate so upstream changes don't conflict.
- `race_features.py` and `race_train.py`: 1 s windows with a 250 ms hop, a
  `DecisionTreeClassifier` (max depth 6), grouped cross-validation by session,
  and export to `dist/race_model.py` plus `dist/report.json`. The deploy tool
  installs it as the Race app's `model.py`.
- Recorded labels are `still`, `walk`, `throw`, `drop` and `unknown`, but the
  model has exactly three states: `still`, `throw` and `drop`. **The badge is
  always in one of them and defaults to `still`.**
- Two stages: the window model labels windows with ≥150 ms of free fall
  (|a| < 0.4 g) in throw/drop data as `throw`, everything else `still`. A
  landing model scores the impact, 0–0.5 s after the last free fall
  (orientation-independent features only), as caught or dropped. `model.Tracker` reports
  `drop` only after a `throw` and only when the shrunk leaf score reaches
  `--drop-confidence` (default 0.85); otherwise the catch is assumed.
- The generated `model.py` must stay plain MicroPython: no numpy and no
  imports beyond `math`.
- Use `python -m unittest discover -s tests` (from `race/`) for tests.

## Connecting to a badge (Windows)

- `mpremote` and `pip` may not be on PATH; use `python -m mpremote` and
  `python -m pip`.
- Find the port with `python -m mpremote devs`; the badge is USB `2e8a:1101`.
  The COM port changes between badges and reconnects, so never hard-code it.
- Then use explicit `python -m mpremote connect <port> ...` commands.
- **Never run serial commands concurrently.** Only one process can own the
  port; close the recorder's serial users, Thonny and Web Serial pages first.
- Remote paths in `mpremote fs` start with `:`.
- `/system` is read-only over serial. `/state` is writable.

## Developing and deploying badge apps

- Apps use runtime globals (`badge`, `screen`, `image`, `font`, `color`,
  `shape`, `run`, `BUTTON_*`); don't import them. `State` comes from
  `badgeware`.
- Use logical buttons (`BUTTON_SELECT` etc.) and `badge.pressed()`/`held()`.
  Use `badge.ticks` and `badge.ticks_delta`, so timing is frame-rate
  independent.
- Read the IMU with `badge.imu()`, which returns `(ax, ay, az, gx, gy, gz)` as
  raw int16. Accel is ±2 g at 16384 LSB/g; the sample rate is the app frame
  rate (about 50 Hz while drawing), so use measured timestamps.
- Each app needs `badge/apps/<name>/__init__.py` and a 24×24 `icon.png`.
- **Deploy Race:** `python tools/deploy_race.py kiosk` installs the app,
  `dist/race_model.py` (as `model.py`), its assets and `badge/main.py`, and
  removes old `airtime`/`leaderboard` copies of the kiosk app. `app` updates
  `/state/apps/race` only; `restore` puts the stock boot back. Wait for the
  port to come back after each reset (`python -m mpremote devs`).
- **Quick test:** `python -m mpremote connect <port> mount badge` exposes
  `badge/` at `/remote`. PNG assets don't stream reliably through the mount.
- Never commit secrets. Keep `badge/secrets.py` empty in `badger/home`.
- Don't commit recorded `data/` or `dist/` unless the user asks; they are in
  `.gitignore`.
