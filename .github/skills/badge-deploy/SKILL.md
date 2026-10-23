---
name: badge-deploy
description: Install, test and debug the Airtime badge app (badge/apps/airtime) on a GitHub Universe badge over USB serial, including autostart, naming, model install and checking it over BLE. Use whenever the user asks to deploy, install, flash, test or debug the app on the badge.
---

# Deploy Airtime to a badge

Read `docs/badge/USB_SERIAL.md` first if you haven't. Paths below are relative
to the repository root.

## 1. Find the badge

```powershell
python -m mpremote devs
```

The badge is USB `2e8a:1101`. The COM port changes between badges and
reconnects, so never hard-code it. Only one process can own the port: close
Thonny, Web Serial pages and any other `mpremote` first, and never run serial
commands in parallel. The recorder only uses BLE, so it can keep running.

## 2. Ask about autostart

Before installing, ask the user whether Airtime should run automatically on
start (use the ask_user tool). Then run one of:

```powershell
python tools/deploy_badge.py --autostart
python tools/deploy_badge.py --no-autostart
```

Add `--port COMx` if more than one badge is attached, and `--name <name>` if
the user wants a name. The tool:

- copies `badge/apps/airtime/*.py` and `icon.png` to `/state/apps/airtime`
  (unchanged files report "Up to date");
- for `--autostart`, patches `/system/main.py` once (the stock file is saved to
  `/state/main.orig.py`) and writes `/state/autostart`;
- for `--no-autostart`, removes `/state/autostart`. The patched `main.py` is
  harmless without it.

Autostart only applies to power-on, the reset button and wake. The boot
animation still plays, HOME still returns to the menu and the badge still
sleeps.

## 3. Start it

Ask the user to **press the badge's reset button**. Don't use
`mpremote reset` to test autostart: it is a watchdog reset, which (like
leaving the app) deliberately boots into the menu.

To watch the app's output instead, use `--run` (Ctrl+C detaches; the app
keeps running). Errors and `print` output from the app appear there.

## 4. Check it

- Over BLE: `python listen.py` prints the badge's adverts (name, state,
  counters). A badge connected to the recorder stops advertising, so stop
  the recorder's connection first or check in the recorder UI instead.
- In the recorder (`python recorder.py`, http://localhost:8765): connect, and
  the log shows the badge's INFO string, e.g.
  `ver=1 id=... uid=... scale=16384 model=bbfc`. `model=err` means the last
  model upload was rejected.
- Read badge files: `python -m mpremote connect <port> fs cat :/state/airtime.json`.

## Badge files

| Path | What |
| --- | --- |
| `/state/apps/airtime/` | dev-deployed app |
| `/state/airtime.json` | settings (`name`, `show_model`), via `badgeware.State` key `airtime` |
| `/state/airtime_model.py` | last model uploaded over BLE (or `--model`) |
| `/state/autostart` | app folder to launch at boot; delete to boot into the menu |
| `/state/main.orig.py` | stock `/system/main.py`, before the autostart patch |

To undo the patch completely, restore `/state/main.orig.py` to
`/system/main.py` the same way `set_autostart()` in `tools/deploy_badge.py`
writes it.

## Gotchas

- `/system` is read-only over serial. To write it, remount it writable, copy
  and unmount **in one mpremote command chain** (see `REMOUNT_SYSTEM` in
  `tools/deploy_badge.py`).
- When writing files with `mpremote exec`, use `with open(...)`. A bare
  `open().write()` may not be flushed before a reset or unmount.
- The boot animation leaves the display in HIRES, so the app sets
  `badge.mode(LORES | VSYNC)` itself. Keep that if you restructure startup.
- `protocol.py` at the repository root and in `badge/apps/airtime/` must stay
  identical. Run `python -m unittest discover -s tests` after changing either,
  or the app's detector.
- For a permanent install (listed in the menu), use USB mass-storage mode and
  copy `badge/apps/airtime` to `system/apps/airtime`.
