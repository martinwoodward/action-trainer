# Copilot instructions for Action Trainer

This repository trains activity models for the **GitHub Universe 2026 badge**.
The laptop side records labelled IMU sessions over Bluetooth, trains a small
decision tree and exports a MicroPython `model.py`. The badge side is the
**Airtime** app in `badge/apps/airtime/`, which streams IMU data, accepts
models over BLE and runs them. Edit Airtime here. The firmware and the other
badge apps live in [`badger/home`](https://github.com/badger/home).

## Before you start

- Read `docs/badge/AGENTS.md` before writing or changing badge app code.
- Use the `badge-deploy` skill (`.github/skills/badge-deploy/SKILL.md`) to
  install, test or debug Airtime on a badge.
- Read `docs/badge/USB_SERIAL.md` before connecting to a badge or changing its
  filesystem.
- Read `docs/badge/HARDWARE.md` before using raw GPIO, I2C, ADC, IR, power,
  wireless, display, touch, switch, or interrupt hardware.
- These are copies of the docs in `badger/home` (`badge/AGENTS.md`,
  `hardware/USB_SERIAL.md`, `hardware/README.md`). If they disagree,
  `badger/home` wins; refresh the copies.

## Repository layout

- `recorder.py` with `web/recorder.html`: a bleak + aiohttp recorder UI at
  http://localhost:8765. Sessions are saved to `data/<label>/*.csv` + `.json`.
- `listen.py`: console listener for badge advertisements.
- `protocol.py`: the advertisement and GATT format.
  `badge/apps/airtime/protocol.py` must be an identical copy (the badge only
  imports from the app folder); edit both together. `tests/test_badge_tools.py`
  enforces this.
- `badge/apps/airtime/`: the badge app (`__init__.py` UI and model runner,
  `ble.py` GATT server, `detect.py` rule-based detector, `icon.png`).
  `tests/test_detect.py` replays it on the laptop.
- `tools/deploy_badge.py`: installs Airtime over USB serial, with optional
  autostart, name and model. `tools/make_icon.py` regenerates the icon.
- `features.py` and `train.py`: 1 s windows with a 250 ms hop, a
  `DecisionTreeClassifier` (max depth 6), grouped cross-validation by session,
  and export to `dist/model.py` plus `dist/report.json`.
- Labels are `still`, `walk`, `throw`, `drop` and `unknown`. **The generated
  model defaults to `still`:** an `unknown` prediction or an empty window
  returns `still`.
- The generated `model.py` must stay plain MicroPython: no numpy and no
  imports beyond `math`.
- Use `python -m unittest discover -s tests` for tests.

## Flashing a badge

When the user asks to flash, install or deploy "the badge" (or similar):

1. **Ask which app**, using the ask_user tool: **Airtime** or **Race**. Don't
   guess, even if the badge currently runs one of them.
2. **Airtime:** follow the `badge-deploy` skill (ask about autostart, then
   `python tools/deploy_badge.py --autostart|--no-autostart`).
3. **Race:** before touching the badge, check for `race/wifi.json`
   (gitignored).
   - If it is missing, copy `race/wifi.example.json` to `race/wifi.json` and
     open it in the OS default editor (macOS `open race/wifi.json`, Windows
     `Start-Process race\wifi.json`, Linux `xdg-open race/wifi.json`). Ask the
     user to fill in `ssid`, `password` and `server` (an IP, `auto` for this
     computer, or `none` to broadcast). Wait until they confirm before
     flashing.
   - If it exists but still has the example values (`MyNetwork` or
     `change-me`), open it the same way and ask the user to finish it.
   - Never print, log or commit the password.
   - Then, from `race/`, run `python tools/deploy_race.py kiosk`. Race boots
     straight into the app, with no boot animation and no menu. It removes
     Airtime from the badge; `python tools/deploy_race.py restore` puts the
     stock boot back.

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
- Airtime must call `badge.mode(LORES | VSYNC)` at start: the boot animation
  leaves the display in HIRES when the app autostarts.
- **Deploy Airtime:** `python tools/deploy_badge.py`, then press the badge's
  reset button. **Whenever you install it, ask the user whether it should
  autostart**, and pass `--autostart` or `--no-autostart`. Autostart must keep
  the boot animation, the reset button, HOME (back to the menu) and sleep
  working.
- **Quick test without copying:** `python -m mpremote connect <port> mount badge`
  exposes `badge/` at `/remote`. PNG assets don't stream reliably through the
  mount.
- **Permanent install:** put the badge in USB mass-storage mode and copy the
  app into `system/apps/`. Eject, then reset the badge.
- **Model deploy:** the recorder's **Train & deploy** sends `dist/model.py`
  over BLE and the app saves it to `/state/airtime_model.py`. Without BLE, use
  `python tools/deploy_badge.py --skip-app --model dist/model.py`. To use a
  model in another app, copy `dist/model.py` into that app in `badger/home`.
- Never commit secrets (Wi-Fi passwords, tokens) to the badge app or tools.
- Don't commit recorded `data/` or `dist/` unless the user asks; they are in
  `.gitignore`.

## Race fork

`race/` is a logical fork focused on a plushy air-time leaderboard, with its
own recorder, trainer, protocol, leaderboard and kiosk badge app. Keep Race
work inside `race/` and follow [`race/AGENTS.md`](../race/AGENTS.md); don't
change the root Airtime files for it.
