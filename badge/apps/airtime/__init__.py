"""Air-time: BLE IMU streamer and throw/drop detector.

Advertises air-time counters as manufacturer data and exposes a GATT service
that streams raw six-axis IMU samples. The laptop-side recorder in
martinwoodward/action-trainer connects to that service to capture labelled
training sessions.

The app shows the generated model's state (still / throw / drop) and has no
exit: on a kiosk badge (`badge/main.py`) it is the only thing that runs.

Deploy with `tools/deploy_badge.py`, which also copies `protocol.py` and
`dist/model.py` from the action-trainer repository.
"""

import os
import sys

APP_DIRS = ("/state/apps/airtime", "/system/apps/airtime")


def _app_dir():
    try:
        here = __file__.rsplit("/", 1)[0]
        if here.startswith("/"):
            return here
    except NameError:
        pass
    for path in APP_DIRS:
        try:
            os.stat(path + "/__init__.py")
            return path
        except OSError:
            pass
    return APP_DIRS[-1]


APP_DIR = _app_dir()

os.chdir(APP_DIR)
sys.path.insert(0, APP_DIR)

import bluetooth
import machine
import math
import struct

from badgeware import State

import protocol

try:
    import model
except ImportError:
    model = None

STILL = 0
WALK = 1
FLYING = 2
THROW = 3
DROP = 4
HANDLING = 5

STATE_NAMES = ("STILL", "WALK", "FLYING", "THROW", "DROP", "HANDLING")
STATE_COLORS = (
    (36, 44, 54),
    (26, 92, 60),
    (28, 76, 150),
    (176, 108, 16),
    (156, 36, 44),
    (72, 56, 104),
)

EV_STEP = 0x01
EV_LAUNCH = 0x02
EV_LAND = 0x04
EV_THROW = 0x08
EV_DROP = 0x10

LSB_PER_G = 16384.0
FREEFALL_G = 0.35
FLIGHT_MIN_MS = 90
LAUNCH_G = 1.9
STILL_TOL_G = 0.09
STILL_GYRO = 2600
STILL_HOLD_MS = 400
STILL_RESET_MS = 3000
STEP_HIGH_G = 1.22
STEP_LOW_G = 0.92
STEP_MIN_MS = 220
WALK_WINDOW_MS = 2200
EVENT_SHOW_MS = 900

MODEL_COLORS = {
    "still": (36, 44, 54),
    "walk": (26, 92, 60),
    "throw": (176, 108, 16),
    "drop": (156, 36, 44),
}

ADV_INTERVAL_US = 250000
PROMPT_FLASH_MS = 500  # recorder prompts show green when they change
ADV_REFRESH_MS = 500
NOTIFY_MIN_MS = 40
MAX_BUFFERED_SAMPLES = 60

_IRQ_CENTRAL_CONNECT = 1
_IRQ_CENTRAL_DISCONNECT = 2
_IRQ_GATTS_WRITE = 3
_IRQ_MTU_EXCHANGED = 21

state_store = {"name": ""}
State.load("airtime", state_store)

badge_id = int.from_bytes(machine.unique_id()[-4:], "little")
if not state_store.get("name"):
    state_store["name"] = "badge-%04x" % (badge_id & 0xFFFF)
badge_name = state_store["name"]

boot_count = state_store.get("boot", 0) + 1
state_store["boot"] = boot_count & 0xFF
State.save("airtime", state_store)


class Detector:
    """Physics-based throw/drop/walk detector over raw IMU samples."""

    def __init__(self):
        self.state = STILL
        self.flight_ms = 0
        self.air_total_ms = 0
        self.air_at_reset_ms = 0
        self.throws = 0
        self.drops = 0
        self.steps = 0
        self._freefall_ms = 0
        self._still_ms = 0
        self._peak_g = 1.0
        self._in_flight = False
        self._event_until = 0
        self._event_state = STILL
        self._step_armed = False
        self._last_step = -STEP_MIN_MS
        self._recent_steps = []

    def update(self, now, dt, ax, ay, az, gx, gy, gz):
        events = 0
        g = math.sqrt(ax * ax + ay * ay + az * az) / LSB_PER_G
        spin = abs(gx) + abs(gy) + abs(gz)

        if g > self._peak_g:
            self._peak_g = g

        if g < FREEFALL_G:
            self._freefall_ms += dt
            if not self._in_flight and self._freefall_ms >= FLIGHT_MIN_MS:
                self._in_flight = True
                events |= EV_LAUNCH
            if self._in_flight:
                self.flight_ms = self._freefall_ms
        else:
            if self._in_flight:
                events |= EV_LAND
                self.flight_ms = self._freefall_ms
                self.air_total_ms += self.flight_ms
                if self._peak_g >= LAUNCH_G:
                    self.throws = (self.throws + 1) & 0xFF
                    self._event_state = THROW
                    events |= EV_THROW
                else:
                    self.drops = (self.drops + 1) & 0xFF
                    self._event_state = DROP
                    events |= EV_DROP
                self._event_until = now + EVENT_SHOW_MS
                self._in_flight = False
            self._freefall_ms = 0
            self._peak_g = g

        if self._step_armed and g < STEP_LOW_G and now - self._last_step >= STEP_MIN_MS:
            self._step_armed = False
            self._last_step = now
            self.steps = (self.steps + 1) & 0xFFFF
            self._recent_steps.append(now)
            events |= EV_STEP
        elif g > STEP_HIGH_G:
            self._step_armed = True

        while self._recent_steps and now - self._recent_steps[0] > WALK_WINDOW_MS:
            self._recent_steps.pop(0)

        if abs(g - 1.0) < STILL_TOL_G and spin < STILL_GYRO:
            self._still_ms += dt
        else:
            self._still_ms = 0

        if self._in_flight:
            self.state = FLYING
        elif now < self._event_until:
            self.state = self._event_state
        elif self._still_ms >= STILL_HOLD_MS:
            self.state = STILL
        elif len(self._recent_steps) >= 2:
            self.state = WALK
        else:
            self.state = HANDLING

        if self._still_ms >= STILL_RESET_MS:
            self.air_at_reset_ms = self.air_total_ms

        return events, g

    @property
    def streak_ms(self):
        return self.air_total_ms - self.air_at_reset_ms


class Link:
    """Raw BLE GATT server: advertising, IMU notifications, control writes."""

    def __init__(self, detector):
        self.detector = detector
        self.connected = set()
        self.prompt = ""
        self.prompt_at = 0
        self.identify_until = 0
        self.mtu = 23
        self.seq = 0
        self.adv_seq = 0
        self._pending = []
        self._last_notify = 0
        self._last_adv = 0
        self._name_dirty = False

        self.ble = bluetooth.BLE()
        self.ble.active(True)
        try:
            self.ble.config(mtu=247)
        except (OSError, ValueError):
            pass
        self.ble.irq(self._irq)

        service = (
            bluetooth.UUID(protocol.SERVICE_UUID),
            (
                (bluetooth.UUID(protocol.STREAM_UUID), bluetooth.FLAG_READ | bluetooth.FLAG_NOTIFY),
                (
                    bluetooth.UUID(protocol.CONTROL_UUID),
                    bluetooth.FLAG_WRITE | bluetooth.FLAG_WRITE_NO_RESPONSE,
                ),
                (bluetooth.UUID(protocol.INFO_UUID), bluetooth.FLAG_READ),
            ),
        )
        ((self.stream_h, self.control_h, self.info_h),) = self.ble.gatts_register_services((service,))
        self.ble.gatts_write(self.info_h, self._info())
        self.ble.gatts_set_buffer(self.control_h, 64, False)
        self.advertise(0)

    def _info(self):
        return ("airtime v%d id=%08x boot=%d lsb_per_g=%d" % (
            protocol.VERSION,
            badge_id,
            boot_count,
            int(LSB_PER_G),
        )).encode()

    @property
    def samples_per_packet(self):
        room = self.mtu - 3 - protocol.STREAM_HEADER_SIZE
        return max(1, room // protocol.SAMPLE_SIZE)

    def _irq(self, event, data):
        if event == _IRQ_CENTRAL_CONNECT:
            self.connected.add(data[0])
        elif event == _IRQ_CENTRAL_DISCONNECT:
            self.connected.discard(data[0])
            self.mtu = 23
            self.prompt = ""
            self._pending = []
        elif event == _IRQ_MTU_EXCHANGED:
            self.mtu = data[1]
        elif event == _IRQ_GATTS_WRITE:
            if data[1] == self.control_h:
                self._control(self.ble.gatts_read(self.control_h))

    def _control(self, payload):
        global badge_name
        if not payload:
            return
        op = payload[:1]
        arg = payload[1:]
        if op == protocol.CMD_LABEL:
            prompt = arg.decode()[:24]
            if prompt != self.prompt:
                self.prompt = prompt
                self.prompt_at = badge.ticks
        elif op == protocol.CMD_IDENTIFY:
            self.identify_until = badge.ticks + 600
        elif op == protocol.CMD_NAME:
            name = arg.decode().strip()[:16]
            if name:
                badge_name = name
                state_store["name"] = name
                self._name_dirty = True

    def advertise(self, now):
        d = self.detector
        self.adv_seq = (self.adv_seq + 1) & 0xFF
        mfg = protocol.pack_mfg(
            badge_id,
            boot_count,
            self.adv_seq,
            shown_state(),
            d.flight_ms,
            d.air_total_ms,
            d.air_at_reset_ms,
            d.drops,
            d.throws,
            d.steps,
        )
        try:
            self.ble.gap_advertise(None)
            self.ble.gap_advertise(
                ADV_INTERVAL_US,
                adv_data=protocol.adv_payload(mfg),
                resp_data=protocol.scan_response(badge_name),
                connectable=True,
            )
        except OSError:
            pass
        self._last_adv = now

    def queue(self, sample):
        if not self.connected:
            return
        self._pending.append(sample)
        if len(self._pending) > MAX_BUFFERED_SAMPLES:
            del self._pending[:-MAX_BUFFERED_SAMPLES]

    def service(self, now):
        if self._name_dirty:
            self._name_dirty = False
            State.save("airtime", state_store)
            self.ble.gatts_write(self.info_h, self._info())
            self.advertise(now)

        if now - self._last_adv >= ADV_REFRESH_MS:
            self.advertise(now)

        if not self.connected or not self._pending:
            return
        if now - self._last_notify < NOTIFY_MIN_MS and len(self._pending) < self.samples_per_packet:
            return

        batch = self._pending[: self.samples_per_packet]
        data = protocol.pack_samples(self.seq, batch)
        sent = False
        for conn in tuple(self.connected):
            try:
                self.ble.gatts_notify(conn, self.stream_h, data)
                sent = True
            except OSError:
                pass
        if sent:
            self.seq = (self.seq + 1) & 0xFFFF
            del self._pending[: len(batch)]
            self._last_notify = now


class Classifier:
    """Wraps the generated model's Tracker (still / throw / drop)."""

    def __init__(self):
        self.tracker = model.Tracker() if model is not None else None
        self.label = "still"

    def update(self, now, sample):
        if self.tracker is not None:
            self.label = self.tracker.update(now, sample)


MODEL_STATES = {"still": STILL, "throw": THROW, "drop": DROP}


def shown_state():
    """State broadcast and streamed: the model's label when a model is loaded."""
    if model is None:
        return detector.state
    return MODEL_STATES.get(classifier.label, STILL)


detector = Detector()
classifier = Classifier()
link = Link(detector)

screen.font = font.sins
_, LINE = screen.measure_text("Ag")
LINE += 2

last_g = 1.0


def draw():
    now = badge.ticks
    if now < link.identify_until:
        screen.pen = color.rgb(255, 255, 255)
        screen.rectangle(screen.clip)
        return

    label = classifier.label if model is not None else STATE_NAMES[detector.state].lower()
    r, g, b = MODEL_COLORS.get(label, STATE_COLORS[detector.state])
    screen.pen = color.rgb(r, g, b)
    screen.rectangle(screen.clip)

    screen.pen = color.rgb(255, 255, 255)
    name = label.upper() if model is not None else "NO MODEL"
    w, _ = screen.measure_text(name)
    screen.text(name, (screen.width - w) // 2, 6)

    screen.pen = color.rgb(210, 220, 230)
    screen.text("air %.2fs" % (detector.streak_ms / 1000), 4, 6 + LINE + 2)
    screen.text("tot %.2fs" % (detector.air_total_ms / 1000), 4, 6 + LINE * 2 + 2)
    screen.text(
        "T%d D%d st%d" % (detector.throws, detector.drops, detector.steps),
        4,
        6 + LINE * 3 + 2,
    )
    screen.text("%.2fg" % last_g, 4, 6 + LINE * 4 + 2)

    link_text = "BLE %s" % ("LINKED" if link.connected else "adv")
    w, _ = screen.measure_text(link_text)
    screen.pen = color.rgb(150, 165, 180)
    screen.text(link_text, screen.width - w - 4, screen.height - LINE - 4)

    if link.prompt:
        w, h = screen.measure_text(link.prompt)
        screen.pen = color.rgb(0, 0, 0, 160)
        screen.shape(shape.rounded_rectangle(2, screen.height - LINE * 2 - 8, w + 10, h + 6, 3))
        if badge.ticks - link.prompt_at < PROMPT_FLASH_MS:
            screen.pen = color.rgb(60, 220, 90)  # new prompt
        else:
            screen.pen = color.rgb(255, 214, 90)
        screen.text(link.prompt, 7, screen.height - LINE * 2 - 5)


def update():
    global last_g

    now = badge.ticks
    dt = badge.ticks_delta
    ax, ay, az, gx, gy, gz = badge.imu()
    events, last_g = detector.update(now, dt, ax, ay, az, gx, gy, gz)
    classifier.update(now, (ax, ay, az, gx, gy, gz))
    link.queue(protocol.pack_sample(now, ax, ay, az, gx, gy, gz, events, shown_state()))
    link.service(now)

    # No exit: handling the plushy brushes the capacitive pads, and the badge
    # is meant to stay in this app. Returning None keeps run() going.

    draw()


run(update)
