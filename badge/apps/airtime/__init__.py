"""Air-time: throw/drop detector that reports to the dashboard over Wi-Fi.

Joins Wi-Fi (settings in /state/airtime_net.json, else /system/secrets.py) and
sends a UDP status datagram (protocol.pack_status) on every state change and
every 250 ms. It sends to the configured dashboard address, or broadcasts
until a dashboard acks. The dashboard can rename the badge, flash its screen
and ping it. This build has no Bluetooth, so the recorder can't connect.

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

import json
import machine
import math
import network
import socket

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

STATUS_EVERY_MS = 250  # heartbeat to the dashboard
STATUS_CHANGE_MS = 100  # a state change is sent at once, at most this often
STATUS_REPEAT_MS = 60  # and repeated once, since Wi-Fi drops a few % of datagrams
WIFI_RETRY_MS = 10000
WIFI_JOIN_TIMEOUT_MS = 20000
SERVER_FORGET_MS = 10000  # no ack for this long: broadcast again
IDENTIFY_MS = 1200  # Find on the dashboard blinks the screen this long
NET_CONFIG = "/state/airtime_net.json"  # {"ssid", "password", "server"}; written by deploy_badge.py

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


class Net:
    """Wi-Fi link to the dashboard: UDP status out, acks and commands in."""

    def __init__(self, detector):
        self.detector = detector
        self.identify_until = 0
        self.seq = 0
        self.sock = None
        self.ip = None
        self.error = None
        self.server = None  # learned from the dashboard's acks
        self.server_heard = 0
        self.sent = 0
        self.failed = 0
        self.joins = 0
        self._last_send = 0
        self._sent_state = None
        self._repeat_at = None
        self._join_at = None
        self._bcast = "255.255.255.255"

        cfg = load_net_config()
        self.ssid = cfg.get("ssid")
        self.password = cfg.get("password", "")
        self.fixed = (cfg["server"], protocol.UDP_PORT) if cfg.get("server") else None

        try:
            network.hostname("airtime-%04x" % (badge_id & 0xFFFF))
        except (AttributeError, OSError, ValueError):
            pass
        self.wlan = network.WLAN(network.STA_IF)
        self.wlan.active(True)
        try:
            # Power saving holds received packets for a beacon interval or more.
            self.wlan.config(pm=getattr(network.WLAN, "PM_PERFORMANCE", 0xA11140))
        except (AttributeError, OSError, ValueError):
            pass
        if not self.ssid:
            self.error = "no WiFi cfg"

    @property
    def online(self):
        return self.sock is not None

    def _join(self, now):
        self._join_at = now
        self.joins += 1
        try:
            self.wlan.disconnect()
        except OSError:
            pass
        try:
            self.wlan.connect(self.ssid, self.password)
        except OSError as exc:
            self.error = "join err %s" % exc

    def _open(self):
        self.ip, mask = self.wlan.ifconfig()[:2]
        try:
            self._bcast = ".".join(
                str(int(a) | (255 - int(m))) for a, m in zip(self.ip.split("."), mask.split("."))
            )
        except ValueError:
            self._bcast = "255.255.255.255"
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        except (AttributeError, OSError):
            pass
        sock.bind(("0.0.0.0", protocol.UDP_PORT))
        sock.setblocking(False)
        self.sock = sock
        self.error = None

    def _close(self):
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
        self.sock = None
        self.ip = None
        self.server = None

    def _wifi(self, now):
        """Join and rejoin without blocking the frame loop."""
        if not self.ssid:
            return
        status = self.wlan.status()
        if status == network.STAT_GOT_IP and self.wlan.isconnected():
            if self.sock is None:
                try:
                    self._open()
                except OSError as exc:
                    self.error = "sock %s" % exc
                    self._close()
            return
        if self.sock is not None:
            self._close()  # lost the network
        if status in (network.STAT_WRONG_PASSWORD, network.STAT_NO_AP_FOUND, network.STAT_CONNECT_FAIL):
            self.error = {
                network.STAT_WRONG_PASSWORD: "bad password",
                network.STAT_NO_AP_FOUND: "no AP",
            }.get(status, "join failed")
        if self._join_at is None or (
            status != network.STAT_CONNECTING and now - self._join_at >= WIFI_RETRY_MS
        ) or now - self._join_at >= WIFI_JOIN_TIMEOUT_MS:
            self._join(now)

    def destination(self, now):
        if self.fixed:
            return self.fixed
        if self.server and now - self.server_heard < SERVER_FORGET_MS:
            return self.server
        return (self._bcast, protocol.UDP_PORT)

    def send_status(self, now):
        d = self.detector
        self.seq = (self.seq + 1) & 0xFFFF
        state = shown_state()
        self._repeat_at = now + STATUS_REPEAT_MS if state != self._sent_state else None
        self._sent_state = state
        try:
            rssi = self.wlan.status("rssi")
        except (OSError, ValueError):
            rssi = 0
        packet = protocol.pack_status(
            badge_id,
            boot_count,
            self.seq,
            self._sent_state,
            d.flight_ms,
            d.air_total_ms,
            d.air_at_reset_ms,
            d.drops,
            d.throws,
            d.steps,
            state_ms[STILL],
            state_ms[THROW],
            state_ms[DROP],
            rssi,
            badge_name,
        )
        try:
            self.sock.sendto(packet, self.destination(now))
            self.sent += 1
        except OSError:
            self.failed += 1  # e.g. ENOMEM while the radio is busy; the next heartbeat retries
        self._last_send = now

    def _receive(self, now):
        for _ in range(4):
            try:
                data, addr = self.sock.recvfrom(128)
            except OSError:
                return
            msg = protocol.unpack_payload(data)
            if msg is None or msg[1] != badge_id:
                continue
            kind, _, payload = msg
            if kind in (protocol.PKT_ACK, protocol.PKT_CMD):
                self.server = (addr[0], protocol.UDP_PORT)
                self.server_heard = now
            if kind == protocol.PKT_CMD and payload:
                self._command(now, payload[:1], payload[1:], addr)

    def _command(self, now, op, arg, addr):
        global badge_name
        if op == protocol.CMD_IDENTIFY:
            self.identify_until = now + IDENTIFY_MS
        elif op == protocol.CMD_NAME:
            name = arg.decode().strip()[: protocol.UDP_NAME_MAX]
            if name and name != badge_name:
                badge_name = name
                state_store["name"] = name
                State.save("airtime", state_store)
            self.send_status(now)
        elif op == protocol.CMD_PING:
            try:
                self.sock.sendto(protocol.pack_reply(badge_id, arg), addr)
            except OSError:
                pass

    def service(self, now):
        self._wifi(now)
        if self.sock is None:
            return
        self._receive(now)
        if now - self._last_send >= STATUS_EVERY_MS or (
            shown_state() != self._sent_state and now - self._last_send >= STATUS_CHANGE_MS
        ) or (self._repeat_at is not None and now >= self._repeat_at):
            self.send_status(now)

    def label(self):
        if self.sock is not None:
            return "WiFi %s%s" % (self.ip, "" if self.server or self.fixed else " bc")
        return "WiFi %s" % (self.error or "...")


def load_net_config():
    """Wi-Fi and server settings: /state/airtime_net.json, then /system/secrets.py."""
    cfg = {}
    try:
        with open(NET_CONFIG) as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        pass
    if not cfg.get("ssid"):
        ns = {}
        try:
            exec(open("/system/secrets.py").read(), ns)
        except (OSError, SyntaxError):
            pass
        if ns.get("WIFI_SSID"):
            cfg["ssid"] = ns["WIFI_SSID"]
            cfg["password"] = ns.get("WIFI_PASSWORD", "")
    return cfg


class Classifier:
    """Wraps the generated model's Tracker (still / throw / drop)."""

    def __init__(self):
        self.tracker = model.Tracker() if model is not None else None
        self.label = "still"

    def update(self, now, sample):
        if self.tracker is not None:
            self.label = self.tracker.update(now, sample)


MODEL_STATES = {"still": STILL, "throw": THROW, "drop": DROP}


# Milliseconds in each model state since boot, advertised in the scan response.
state_ms = {STILL: 0, THROW: 0, DROP: 0}
# Heuristic-detector states (no model loaded) folded into the model's three.
STATE_BUCKET = {FLYING: THROW, THROW: THROW, DROP: DROP}


def shown_state():
    """State broadcast and streamed: the model's label when a model is loaded."""
    if model is None:
        return detector.state
    return MODEL_STATES.get(classifier.label, STILL)


detector = Detector()
classifier = Classifier()
link = Net(detector)

# Look of the dashboard's card for this badge (web/dashboard.html): its
# light theme, the badge's colour and its namesake's picture. Keep PALETTE and
# NAMESAKES in step with the page.
PALETTE = (
    (8, 135, 43), (9, 105, 218), (188, 76, 0), (130, 80, 223), (191, 57, 137),
    (207, 34, 46), (27, 124, 131), (154, 103, 0), (90, 97, 92), (49, 146, 170),
)
# (word in the name, PALETTE index, picture in assets/). copilot.png and
# mona.png are Primer Octicons (MIT); squirrel.png is Twemoji 1f43f (CC-BY 4.0).
NAMESAKES = (
    ("copilot", 1, "copilot"),
    ("mona", 3, "mona"),
    ("octocat", 3, "mona"),
    ("duck", 2, "duck"),
    ("squirrel", 4, "squirrel"),
)
BG = (255, 255, 255)
INK = (16, 20, 17)
MUTED = (88, 96, 90)
DROP_RGB = (207, 34, 46)

NAME_FONTS = (font.absolute, font.nope, font.sins)
AIR_FONT = font.ziplock
LABEL_FONT = font.sins
PILL_FONT = font.torch

_theme = {"name": None}


def theme():
    """The badge's colour and picture, worked out again only after a rename."""
    if _theme["name"] != badge_name:
        key = badge_name.lower()
        hit = None
        for word, index, picture in NAMESAKES:
            if word in key:
                hit = (index, picture)
                break
        _theme["name"] = badge_name
        _theme["rgb"] = PALETTE[hit[0] if hit else badge_id % len(PALETTE)]
        _theme["icon"] = None
        if hit:
            try:
                _theme["icon"] = image.load("assets/%s.png" % hit[1])
            except (OSError, ValueError):
                pass
    return _theme


def fmt_air(ms):
    """Air time as the dashboard shows it: 12.3 s, 4:05 or 1:02:03."""
    sec = ms / 1000
    if sec < 60:
        return "%.1f s" % sec
    sec = int(sec)
    h, m, r = sec // 3600, sec // 60 % 60, sec % 60
    return "%d:%02d:%02d" % (h, m, r) if h else "%d:%02d" % (m, r)


def pen(rgb):
    screen.pen = color.rgb(*rgb)


def pill(text, rgb, right, y):
    screen.font = PILL_FONT
    w, h = screen.measure_text(text)
    x = right - int(w) - 8
    pen(rgb)
    screen.shape(shape.rounded_rectangle(x, y, int(w) + 8, int(h) + 4, 3))
    pen((255, 255, 255))
    screen.text(text, x + 4, y + 2)


BATTERY_EVERY_MS = 2000
LOW_BATTERY = 20  # percent; drawn in red at or below this
_battery = {"at": None, "level": None, "charging": False}


def battery():
    """Battery percent and charging flag, read at most every 2 s."""
    now = badge.ticks
    if _battery["at"] is None or now - _battery["at"] >= BATTERY_EVERY_MS:
        _battery["at"] = now
        try:
            _battery["level"] = max(0, min(100, int(badge.battery_level())))
            _battery["charging"] = bool(badge.is_charging())
        except (AttributeError, OSError, ValueError):
            _battery["level"] = None
    return _battery


CHARGE_STEP_MS = 300  # the charging animation adds a fifth of the fill per step


def draw_battery(right, top):
    """Battery icon ending at `right`; returns its left edge.

    On battery it shows the percentage; while charging the reading is
    meaningless, so the icon just fills up over and over instead.
    """
    b = battery()
    if b["level"] is None:
        return right
    level = b["level"]
    screen.font = LABEL_FONT
    _, th = screen.measure_text("0")
    if b["charging"]:
        tw = -3  # no text; the icon sits flush with `right`
        width = 2 * (badge.ticks // CHARGE_STEP_MS % 6)  # 0, 2, ... 10
        fill = INK
    else:
        text = "%d%%" % level
        tw, _ = screen.measure_text(text)
        tw = int(tw)
        width = max(1, 10 * level // 100)
        fill = DROP_RGB if level <= LOW_BATTERY else INK
        pen(MUTED)
        screen.text(text, right - tw, top)
    # Body 14x8 with a 2x4 nub, left of the text.
    bx = right - tw - 21
    by = top + int(th) // 2 - 4
    pen(MUTED)
    screen.rectangle(bx, by, 14, 8)
    screen.rectangle(bx + 14, by + 2, 2, 4)
    pen(BG)
    screen.rectangle(bx + 1, by + 1, 12, 6)
    if width:
        pen(fill)
        screen.rectangle(bx + 2, by + 2, width, 4)
    return bx


last_g = 1.0


def draw():
    now = badge.ticks
    t = theme()
    rgb = t["rgb"]
    if now < link.identify_until:
        pen(rgb if (link.identify_until - now) // 150 % 2 else BG)
        screen.rectangle(screen.clip)
        return

    pen(BG)
    screen.rectangle(screen.clip)

    # Header: colour swatch, picture and name, as on the dashboard card.
    pen(rgb)
    screen.shape(shape.rounded_rectangle(6, 14, 7, 7, 2))
    x = 18
    if t["icon"] is not None:
        screen.blit(t["icon"], vec2(x, 5))
        x += 28
    name_right = draw_battery(screen.width - 6, 11) - 4
    pen(INK)
    for f in NAME_FONTS:
        screen.font = f
        w, h = screen.measure_text(badge_name)
        if w <= name_right - x:
            break
    shown_name = badge_name
    while w > name_right - x and len(shown_name) > 1:
        shown_name = shown_name[:-1]
        w, h = screen.measure_text(shown_name + "..")
    if shown_name != badge_name:
        shown_name += ".."
    screen.text(shown_name, x, 17 - int(h) // 2)

    # Air time since the badge started, with the card's colour bar.
    pen(rgb)
    screen.rectangle(8, 40, 2, 42)
    pen(INK)
    screen.font = AIR_FONT
    screen.text(fmt_air(state_ms[THROW]), 16, 42)
    pen(MUTED)
    screen.font = LABEL_FONT
    screen.text("AIR TIME", 16, 68)

    shown = shown_state()
    if shown == THROW:
        pill("IN THE AIR", rgb, screen.width - 6, 66)
    elif shown == DROP:
        pill("DROPPED", DROP_RGB, screen.width - 6, 66)

    # Footer: counts and the Wi-Fi link, muted like the card's meta line.
    pen(MUTED)
    screen.font = LABEL_FONT
    screen.text("T%d D%d" % (detector.throws, detector.drops), 6, screen.height - 16)
    link_text = link.label()
    w, _ = screen.measure_text(link_text)
    screen.text(link_text, screen.width - int(w) - 6, screen.height - 16)
    if model is None:
        pill("NO MODEL", DROP_RGB, screen.width - 6, 88)


def update():
    global last_g

    now = badge.ticks
    dt = badge.ticks_delta
    ax, ay, az, gx, gy, gz = badge.imu()
    _, last_g = detector.update(now, dt, ax, ay, az, gx, gy, gz)
    classifier.update(now, (ax, ay, az, gx, gy, gz))
    shown = shown_state()
    bucket = STATE_BUCKET.get(shown, STILL)
    state_ms[bucket] += dt
    link.service(now)

    # No exit: handling the plushy brushes the capacitive pads, and the badge
    # is meant to stay in this app. Returning None keeps run() going.

    draw()


run(update)
