import os
import sys

APP_DIRS = ("/remote/apps/airtime", "/state/apps/airtime", "/system/apps/airtime", "/apps/airtime", "/airtime")
APP_DIR = next(path for path in APP_DIRS if is_dir(path))
os.chdir(APP_DIR)
sys.path.insert(0, APP_DIR)

# The boot animation leaves the display in HIRES when we autostart.
badge.mode(LORES | VSYNC)

import math
import random

from badgeware import State

import detect
from ble import Radio
from protocol import (
    CMD_IDENTIFY,
    CMD_LABEL,
    CMD_MODEL_BEGIN,
    CMD_MODEL_DATA,
    CMD_MODEL_END,
    CMD_MODEL_SHOW,
    CMD_NAME,
    model_checksum,
    pack_mfg,
    pack_sample,
)

STATE_KEY = "airtime"
MODEL_PATH = "/state/airtime_model.py"
MODEL_WINDOW_MS = 1000
MODEL_HOP_MS = 250
STEP_POLL_MS = 250
ADV_REFRESH_MS = 100
IDENTIFY_MS = 1500

STYLE = {
    detect.STILL: (color.rgb(30, 70, 200), "STILL"),
    detect.WALK: (color.rgb(30, 150, 60), "WALK"),
    detect.FLYING: (color.rgb(230, 200, 20), "FLYING!"),
    detect.THROW: (color.rgb(240, 120, 20), "THROW"),
    detect.DROP: (color.rgb(210, 30, 30), "DROP!"),
    detect.HANDLING: (color.rgb(90, 90, 100), "..."),
}
WHITE = color.rgb(255, 255, 255)
BLACK = color.rgb(0, 0, 0)

uid = badge.uid if isinstance(badge.uid, str) else badge.uid()
settings = {"name": "", "show_model": True}
State.load(STATE_KEY, settings)
name = settings.get("name") or "AT-" + uid[-4:]
badge_id = int(uid[-8:], 16)
boot = random.getrandbits(8)

det = detect.Detector()
radio = None
radio_error = None
seq = 0
last_adv = 0
last_steps_poll = 0
last_state = None
debug = False
mag_g = 0.0
label = ""
identify_until = 0

model_predict = None
model_tag = None
model_upload = None
model_window = []
model_action = None
last_model_run = 0


def load_model(source):
    scope = {}
    exec(source, scope)
    return scope["predict_window"]


def load_saved_model():
    global model_predict, model_tag
    try:
        with open(MODEL_PATH, "rb") as file:
            source = file.read()
        model_predict = load_model(source)
        model_tag = "%04x" % model_checksum(source)
    except Exception as e:  # noqa: BLE001 - a bad model must not stop the app
        if not isinstance(e, OSError):
            print("airtime: saved model failed:", e)
        model_predict = model_tag = None


def info_text(model=None):
    text = "ver=1 id=%08x uid=%s scale=16384" % (badge_id, uid)
    model = model or model_tag
    return (text + " model=" + model) if model else text


def set_show_model(show):
    if settings.get("show_model", True) != show:
        settings["show_model"] = show
        State.save(STATE_KEY, settings)


def finish_model(arg):
    """Verify, save and load an uploaded model; report the result in INFO."""
    global model_predict, model_tag, model_upload, model_action
    source, model_upload = bytes(model_upload or b""), None
    try:
        length, checksum = (int(v) for v in arg.decode().split(","))
        if len(source) != length or model_checksum(source) != checksum:
            raise ValueError("got %d bytes, sum %d" % (len(source), model_checksum(source)))
        predict = load_model(source)
        with open(MODEL_PATH, "wb") as file:
            file.write(source)
    except Exception as e:  # noqa: BLE001
        print("airtime: model rejected:", e)
        radio.set_info(info_text("err"))
        return
    model_predict, model_tag, model_action = predict, "%04x" % checksum, None
    set_show_model(True)
    radio.set_info(info_text())


load_saved_model()

def start_radio():
    global radio, radio_error
    try:
        radio = Radio(name, info_text().encode())
    except Exception as e:  # keep the display working without BLE
        radio_error = repr(e)


def handle_command(cmd, now):
    global name, label, identify_until, model_upload
    op, raw = cmd[:1], cmd[1:]
    if op == CMD_MODEL_DATA:
        if model_upload is not None:
            model_upload.extend(raw)
        return
    if op == CMD_MODEL_BEGIN:
        model_upload = bytearray()
        radio.set_info(info_text())
        return
    if op == CMD_MODEL_END:
        finish_model(raw)
        return
    arg = raw.decode()
    if op == CMD_NAME and arg:
        name = arg[:16]
        settings["name"] = name
        State.save(STATE_KEY, settings)
        radio.set_name(name)
    elif op == CMD_MODEL_SHOW:
        set_show_model(arg != "0")
    elif op == CMD_LABEL:
        label = arg[:20]
    elif op == CMD_IDENTIFY:
        identify_until = now + IDENTIFY_MS


def read_steps():
    try:
        return badge._imu.get_step_count()
    except Exception:
        return None


def publish(now):
    global seq
    if radio is None:
        return
    seq += 1
    radio.publish(
        pack_mfg(
            badge_id,
            boot,
            seq,
            det.state,
            det.flight_ms(now),
            det.air_total_ms,
            det.air_at_reset_ms,
            det.drops,
            det.throws,
            det.steps,
        )
    )


def centre(text, y):
    w, _ = screen.measure_text(text)
    screen.text(text, (screen.width - w) / 2, y)


def draw(now):
    if now < identify_until:
        screen.pen = WHITE if (now // 150) % 2 else BLACK
        screen.clear()
        return

    bg, text = STYLE[det.state]
    screen.pen = bg
    screen.clear()
    ink = BLACK if det.state == detect.FLYING else WHITE

    screen.pen = ink
    screen.font = font.ark
    centre(name, 4)

    screen.font = font.ignore
    centre(text, 38)

    screen.font = font.ark
    if det.state in (detect.FLYING, detect.THROW):
        centre("%d ms" % det.flight_ms(now), 72)
    elif det.state == detect.WALK:
        centre("%d steps" % det.steps, 72)

    if radio is not None and radio.connected:
        screen.pen = BLACK
        screen.rectangle(0, screen.height - 18, screen.width, 18)
        screen.pen = color.rgb(255, 60, 60)
        screen.text("REC", 2, screen.height - 16)
        screen.pen = WHITE
        centre(label or "connected", screen.height - 16)
    else:
        centre("air %.2fs  drops %d" % (det.streak_ms(now) / 1000, det.drops), screen.height - 16)

    if debug:
        screen.font = font.sins
        screen.text("|a| %.2f thr %d rst %d %s" % (mag_g, det.throws, det.still_resets, "BLE" if radio else "noBLE"), 2, 88)
        if radio_error:
            screen.text(radio_error[:30], 2, 100)
    elif model_action and settings.get("show_model", True):
        screen.pen = ink
        screen.font = font.sins
        centre("model: " + model_action, 88)


start_radio()


def run_model(now, sample):
    """Classify the last second of IMU samples once per hop."""
    global model_predict, model_action, last_model_run
    if model_predict is None:
        return
    model_window.append((now, sample))
    while model_window and now - model_window[0][0] >= MODEL_WINDOW_MS:
        model_window.pop(0)
    if now - last_model_run < MODEL_HOP_MS or len(model_window) < 2:
        return
    last_model_run = now
    try:
        model_action = model_predict([s for _, s in model_window])
    except Exception as e:  # noqa: BLE001 - drop a broken model, keep the app
        print("airtime: model failed:", e)
        model_predict = model_action = None


def update():
    global last_adv, last_steps_poll, last_state, debug, mag_g, label
    now = badge.ticks

    if badge.pressed(BUTTON_SELECT):
        debug = not debug

    ax, ay, az, gx, gy, gz = badge.imu()
    events = getattr(badge, "_imu_events", 0) or 0
    mag_g = math.sqrt(ax * ax + ay * ay + az * az) / detect.LSB_PER_G

    if now - last_steps_poll >= STEP_POLL_MS:
        last_steps_poll = now
        count = read_steps()
        if count is not None:
            det.feed_steps(now, count)

    det.update(now, ax, ay, az, gx, gy, gz, events)
    run_model(now, (ax, ay, az, gx, gy, gz))

    if radio is not None:
        radio.check_link()
        while radio.commands:
            handle_command(radio.commands.pop(0), now)
        if radio.connected:
            radio.stream(pack_sample(now, ax, ay, az, gx, gy, gz, events, det.state))
        elif label:
            label = ""

    if det.state != last_state or det.events or now - last_adv >= ADV_REFRESH_MS:
        last_state = det.state
        last_adv = now
        publish(now)

    draw(now)


def on_exit():
    if radio is not None:
        radio.close()


run(update)
