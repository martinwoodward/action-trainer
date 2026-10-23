"""Replay synthetic IMU traces through the badge detector and protocol.

Run:  python -m unittest discover -s tests
"""
import random
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "badge" / "apps" / "airtime"))

import detect  # noqa: E402
import protocol  # noqa: E402

G = 16384
DT = 11  # ~90 fps, as measured on hardware
GYRO_BIAS = (160, -530, -133)


class Trace:
    def __init__(self, seed=1):
        self.t = 0
        self.rng = random.Random(seed)
        self.det = detect.Detector()
        self.events = []

    def _feed(self, ax, ay, az, gx=0, gy=0, gz=0, ev=0):
        self.t += DT
        self.det.update(
            self.t, int(ax), int(ay), int(az), GYRO_BIAS[0] + gx, GYRO_BIAS[1] + gy, GYRO_BIAS[2] + gz, ev
        )
        self.events.extend(self.det.events)

    def rest(self, ms):
        for _ in range(ms // DT):
            n = self.rng.randint
            self._feed(n(-40, 40), n(-40, 40), G + n(-40, 40), n(-5, 5), n(-5, 5), n(-5, 5))

    def hand(self, ms):
        for _ in range(ms // DT):
            n = self.rng.randint
            self._feed(n(-2500, 2500), n(-2500, 2500), G + n(-2500, 2500), n(-400, 400), n(-400, 400), n(-400, 400))

    def launch(self, ms=150):
        for _ in range(ms // DT):
            self._feed(0, 8000, 2 * G, 3000, 0, 0)

    def fly(self, ms, freefall_bit=True):
        for _ in range(ms // DT):
            self._feed(150, -200, 250, 2000, 1500, 0, detect.FREEFALL_BIT if freefall_bit else 0)

    def impact(self, ms=33):
        for _ in range(ms // DT):
            self._feed(9000, -32000, 32000, 8000, 0, 0)

    def shake(self, ms):
        for i in range(ms // DT):
            g = (0.23, 3.3)[i % 2]
            self._feed(0, 0, g * G, 5000, 0, 0)


class DetectorTests(unittest.TestCase):
    def test_rest_is_still_and_resets_after_3s(self):
        tr = Trace()
        tr.rest(3500)
        self.assertEqual(tr.det.state, detect.STILL)
        self.assertIn("still_reset", tr.events)

    def test_drop_from_hand(self):
        tr = Trace()
        tr.hand(1000)
        tr.fly(250)
        tr.impact()
        tr.rest(1200)
        self.assertEqual((tr.det.throws, tr.det.drops), (0, 1))
        self.assertAlmostEqual(tr.det.air_total_ms, 253, delta=2 * DT)
        self.assertIn("drop", tr.events)

    def test_drop_with_bounce_counts_once(self):
        tr = Trace()
        tr.hand(1000)
        tr.fly(300)
        tr.impact()
        tr.fly(80, freefall_bit=False)
        tr.impact()
        tr.rest(1200)
        self.assertEqual((tr.det.throws, tr.det.drops), (0, 1))

    def test_throw_and_catch(self):
        tr = Trace()
        tr.hand(1000)
        tr.launch()
        tr.fly(600)
        tr.impact()
        tr.hand(2500)
        self.assertEqual((tr.det.throws, tr.det.drops), (1, 0))
        self.assertAlmostEqual(tr.det.last_flight_ms, 605, delta=2 * DT)

    def test_throw_onto_floor_is_throw_and_drop(self):
        tr = Trace()
        tr.hand(1000)
        tr.launch()
        tr.fly(500)
        tr.impact()
        tr.rest(1200)
        self.assertEqual((tr.det.throws, tr.det.drops), (1, 1))

    def test_catch_then_rethrow_counts_two_throws(self):
        tr = Trace()
        tr.hand(500)
        tr.launch()
        tr.fly(400)
        tr.impact()
        tr.hand(800)
        tr.launch()
        tr.fly(400)
        tr.impact()
        tr.hand(2500)
        self.assertEqual((tr.det.throws, tr.det.drops), (2, 0))

    def test_flying_state_and_live_streak(self):
        tr = Trace()
        tr.hand(500)
        tr.launch()
        tr.fly(400)
        self.assertEqual(tr.det.state, detect.FLYING)
        self.assertGreater(tr.det.streak_ms(tr.t), 350)

    def test_still_reset_zeroes_streak_but_keeps_total(self):
        tr = Trace()
        tr.hand(500)
        tr.launch()
        tr.fly(500)
        tr.impact()
        tr.hand(2500)
        self.assertGreater(tr.det.streak_ms(tr.t), 400)
        tr.rest(3200)
        self.assertEqual(tr.det.streak_ms(tr.t), 0)
        self.assertGreater(tr.det.air_total_ms, 400)

    def test_held_in_hand_does_not_reset(self):
        tr = Trace()
        tr.hand(500)
        tr.launch()
        tr.fly(500)
        tr.impact()
        tr.hand(5000)
        self.assertGreater(tr.det.streak_ms(tr.t), 400)

    def test_shake_is_not_flight(self):
        tr = Trace()
        tr.shake(3000)
        self.assertEqual(tr.det.air_total_ms, 0)
        self.assertEqual((tr.det.throws, tr.det.drops), (0, 0))

    def test_short_jolt_ignored(self):
        tr = Trace()
        tr.hand(500)
        tr.fly(66)
        tr.impact()
        tr.hand(2500)
        self.assertEqual(tr.det.air_total_ms, 0)

    def test_steps_only_while_walking(self):
        tr = Trace()
        tr.det.feed_steps(0, 100)
        tr.det.feed_steps(250, 101)
        self.assertEqual(tr.det.steps, 0)
        for i in range(2, 10):
            tr.det.feed_steps(i * 250, 100 + i)
        self.assertGreater(tr.det.steps, 0)
        tr.det.update(10 * 250, 3000, 0, G, 300, 0, 0)
        self.assertEqual(tr.det.state, detect.WALK)


class ProtocolTests(unittest.TestCase):
    def test_roundtrip_and_fits_legacy_advert(self):
        mfg = protocol.pack_mfg(0x1234ABCD, 7, 300, detect.DROP, 70000, 123456, 1000, 300, 2, 65)
        adv = protocol.adv_payload(mfg)
        self.assertLessEqual(len(adv), 31)
        d = protocol.unpack_mfg(mfg)
        self.assertEqual(d["badge_id"], 0x1234ABCD)
        self.assertEqual(d["seq"], 300 & 0xFF)
        self.assertEqual(d["flight_ms"], 0xFFFF)
        self.assertEqual(d["drops"], 300 & 0xFF)
        self.assertEqual(d["state"], detect.DROP)

    def test_rejects_foreign_data(self):
        self.assertIsNone(protocol.unpack_mfg(b"\xff\xffXX" + bytes(30)))
        self.assertIsNone(protocol.unpack_mfg(b"\x4c\x00"))

    def test_scan_response_name(self):
        self.assertEqual(protocol.scan_response("AT-0ee5"), b"\x08\x09AT-0ee5")


if __name__ == "__main__":
    unittest.main()
