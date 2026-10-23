"""Physics-only motion detector for the air-time demo.

Pure Python (no badge globals) so the same file runs on the badge and on the
laptop for replay tests. Feed one IMU sample per frame via ``update()``.
"""

import math

STILL = 0
WALK = 1
FLYING = 2
THROW = 3
DROP = 4
HANDLING = 5
STATE_NAMES = ("STILL", "WALK", "FLYING", "THROW", "DROP", "HANDLING")

LSB_PER_G = 16384.0
FREEFALL_BIT = 0x04

FLIGHT_START_G = 0.3
FLIGHT_START_SAMPLES = 2
FLIGHT_END_G = 0.6
MIN_FLIGHT_MS = 120
MAX_FLIGHT_MS = 5000

LAUNCH_G = 1.6
LAUNCH_WINDOW_MS = 400
SETTLE_MS = 750
SETTLE_GIVE_UP_MS = 2000
BOUNCE_MS = 600
SETTLE_TOL_LSB = int(0.08 * LSB_PER_G)
GYRO_TOL_LSB = 60

STILL_TOL_LSB = int(0.06 * LSB_PER_G)
STILL_SHOW_MS = 500
STILL_RESET_MS = 3000

EVENT_SHOW_MS = 1500
WALK_WINDOW_MS = 2000
WALK_MIN_STEPS = 3


def _moved(ref, s, tol):
    if ref is None:
        return True
    return (
        max(abs(s[0] - ref[0]), abs(s[1] - ref[1]), abs(s[2] - ref[2])) > tol
        or max(abs(s[3] - ref[3]), abs(s[4] - ref[4]), abs(s[5] - ref[5])) > GYRO_TOL_LSB
    )


class Detector:
    """Tracks flights, landings, stillness and walking.

    Counters only ever increase so a listener that misses packets loses
    nothing; the streak is ``air_total_ms - air_at_reset_ms``.
    """

    def __init__(self):
        self.state = HANDLING
        self.air_total_ms = 0
        self.air_at_reset_ms = 0
        self.still_resets = 0
        self.throws = 0
        self.drops = 0
        self.steps = 0
        self.last_flight_ms = 0
        self.events = []

        self._low_count = 0
        self._low_start = 0
        self._flight_start = None
        self._flight_launched = False
        self._carry = None
        self._last_launch_t = None
        self._landing = None
        self._event_until = 0
        self._event_state = None

        self._ref = None
        self._still_since = 0
        self._reset_done = False

        self._hw_steps_prev = None
        self._step_times = []

    def flying(self):
        return self._flight_start is not None

    def flight_ms(self, now):
        if self._flight_start is None:
            return self.last_flight_ms
        return max(0, now - self._flight_start)

    def streak_ms(self, now):
        live = self.flight_ms(now) if self._flight_start is not None else 0
        return self.air_total_ms - self.air_at_reset_ms + live

    def feed_steps(self, now, count):
        """Feed the cumulative hardware pedometer count (every ~250 ms)."""
        prev = self._hw_steps_prev
        self._hw_steps_prev = count
        if prev is None:
            return
        delta = count - prev
        if delta < 0:
            delta = count
        delta = min(delta, 10)
        for _ in range(delta):
            self._step_times.append(now)
        if delta and len(self._step_times) >= WALK_MIN_STEPS and self._flight_start is None:
            self.steps += delta

    def update(self, now, ax, ay, az, gx=0, gy=0, gz=0, events=0):
        self.events = []
        mag = math.sqrt(ax * ax + ay * ay + az * az) / LSB_PER_G
        s = (ax, ay, az, gx, gy, gz)

        if mag >= LAUNCH_G:
            self._last_launch_t = now

        self._update_flight(now, mag, events)
        self._update_landing(now, s)
        self._update_still(now, s)

        cutoff = now - WALK_WINDOW_MS
        while self._step_times and self._step_times[0] < cutoff:
            self._step_times.pop(0)

        self.state = self._derive_state(now)
        return self.state

    def _update_flight(self, now, mag, events):
        if self._flight_start is None:
            if mag < FLIGHT_START_G or (events & FREEFALL_BIT):
                if self._low_count == 0:
                    self._low_start = now
                self._low_count += 1
                if self._low_count >= FLIGHT_START_SAMPLES:
                    self._flight_start = self._low_start
                    launch = self._last_launch_t
                    self._flight_launched = launch is not None and self._low_start - launch <= LAUNCH_WINDOW_MS
                    land = self._landing
                    self._carry = None
                    if land is not None:
                        if self._flight_launched and now - land[0] > BOUNCE_MS:
                            # Caught, then thrown again.
                            self._resolve_landing(now, settled=False)
                        else:
                            # Bounce or slip: same event continues.
                            self._flight_launched = land[1]
                            self._carry = land
                            self._landing = None
            else:
                self._low_count = 0
            return

        duration = now - self._flight_start
        if mag > FLIGHT_END_G or duration > MAX_FLIGHT_MS:
            self._flight_start = None
            self._low_count = 0
            if MIN_FLIGHT_MS <= duration <= MAX_FLIGHT_MS:
                self.air_total_ms += duration
                self.last_flight_ms = duration
                self._landing = [now, self._flight_launched, None, now]
            elif self._carry is not None:
                self._landing = [now, self._carry[1], None, now]
            self._carry = None

    def _update_landing(self, now, s):
        land = self._landing
        if land is None or self._flight_start is not None:
            return
        if now - land[0] < 100:
            return
        if _moved(land[2], s, SETTLE_TOL_LSB):
            land[2] = s
            land[3] = now
        if now - land[3] >= SETTLE_MS:
            self._resolve_landing(now, settled=True)
        elif now - land[0] >= SETTLE_GIVE_UP_MS:
            self._resolve_landing(now, settled=False)

    def _resolve_landing(self, now, settled):
        land = self._landing
        self._landing = None
        if land[1] or not settled:
            self.throws += 1
            self._emit(now, THROW)
        if settled:
            self.drops += 1
            self._emit(now, DROP)

    def _update_still(self, now, s):
        if self._flight_start is not None or _moved(self._ref, s, STILL_TOL_LSB):
            self._ref = s
            self._still_since = now
            self._reset_done = False
            return
        if not self._reset_done and now - self._still_since >= STILL_RESET_MS:
            self._reset_done = True
            self.air_at_reset_ms = self.air_total_ms
            self.still_resets += 1
            self.events.append("still_reset")

    def _emit(self, now, state):
        self.events.append(STATE_NAMES[state].lower())
        self._event_state = state
        self._event_until = now + EVENT_SHOW_MS

    def _derive_state(self, now):
        if self._flight_start is not None:
            return FLYING
        if self._event_state is not None and now < self._event_until:
            return self._event_state
        self._event_state = None
        if self._landing is not None:
            return HANDLING
        if now - self._still_since >= STILL_SHOW_MS:
            return STILL
        if len(self._step_times) >= WALK_MIN_STEPS:
            return WALK
        return HANDLING
