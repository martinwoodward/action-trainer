"""Per-state time totals: scan-response format and dashboard aggregation."""
import tempfile
import time
import unittest
from pathlib import Path

import protocol
from dashboard import Dashboard


def mfg(boot, state=0, badge_id=0xC03CFD52):
    return {"badge_id": badge_id, "boot": boot, "state": state}


def stats(boot, still, throw=0, drop=0):
    return {"boot": boot, "still_ms": still, "throw_ms": throw, "drop_ms": drop}


class StatsFormatTests(unittest.TestCase):
    def test_scan_response_fits_and_round_trips(self):
        packed = protocol.pack_stats(7, 123456, 789, 4242)
        resp = protocol.scan_response("a-very-long-badge-name", packed)
        self.assertLessEqual(len(resp), 31)
        # Scanners hand over the service data without its 16-bit UUID.
        self.assertEqual(protocol.unpack_stats(packed[2:]), stats(7, 123456, 789, 4242))
        self.assertIsNone(protocol.unpack_stats(None))


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state_file = Path(self.temp.name) / "state.json"
        self.dash = Dashboard(self.state_file)

    def tearDown(self):
        self.temp.cleanup()

    def totals(self):
        return self.dash.snapshot(now=100.0)["badges"][0]["totals"]

    def test_page_reset_subtracts_and_survives_restart(self):
        self.dash.observe("A", "b1", -50, mfg(3), stats(3, 10000, 500), now=100.0)
        self.assertTrue(self.dash.reset("c03cfd52"))
        self.dash.observe("A", "b1", -50, mfg(3, state=4), stats(3, 12000, 1500, 2000), now=100.0)
        self.assertEqual(self.totals(), {"still": 2000, "throw": 1000, "drop": 2000})

        restarted = Dashboard(self.state_file)
        restarted.observe("A", "b1", -50, mfg(3), stats(3, 12000, 1500, 2000), now=100.0)
        self.assertEqual(restarted.snapshot(now=100.0)["badges"][0]["totals"]["still"], 2000)

    def test_reboot_clears_page_reset_and_old_totals(self):
        self.dash.observe("A", "b1", -50, mfg(3), stats(3, 10000), now=100.0)
        self.dash.reset("c03cfd52")
        # New boot, but the scan response still carries the previous boot's totals.
        self.dash.observe("A", "b1", -50, mfg(4), stats(3, 10000), now=100.0)
        self.assertIsNone(self.totals())
        self.dash.observe("A", "b1", -50, mfg(4), stats(4, 300), now=100.0)
        self.assertEqual(self.totals(), {"still": 300, "throw": 0, "drop": 0})
        self.assertNotIn("c03cfd52", Dashboard(self.state_file).baselines)

    def test_history_samples_air_time_and_collapses_flat_runs(self):
        for i, throw in enumerate([0, 0, 0, 800, 800]):
            self.dash.observe("A", "b1", -50, mfg(3), stats(3, 1000, throw), now=100.0 + i)
            self.dash.sample(now=100.0 + i)
        self.assertEqual(self.dash.history["c03cfd52"],
                         [[100000, 0], [102000, 0], [103000, 800], [104000, 800]])
        self.dash.sample(now=200.0)  # nothing new heard: no new point
        self.assertEqual(len(self.dash.history["c03cfd52"]), 4)

    def test_return_after_gap_continues_the_same_line_and_persists(self):
        t0 = float(int(time.time()) - 400)
        ms = lambda t: int((t0 + t) * 1000)
        self.dash.observe("A", "b1", -50, mfg(3), stats(3, 1000, 800), now=t0)
        self.dash.sample(now=t0)
        for t in (200.0, 201.0, 202.0):  # back in range after 200 s
            self.dash.observe("A", "b1", -50, mfg(3), stats(3, 1000, 2500), now=t0 + t)
            self.dash.sample(now=t0 + t)
        self.assertEqual(self.dash.history["c03cfd52"],
                         [[ms(0), 800], [ms(200), 2500, 1], [ms(202), 2500]])
        self.dash.save()
        restarted = Dashboard(self.state_file)
        self.assertEqual(restarted.history, self.dash.history)
        self.assertEqual(restarted.snapshot(now=t0 + 202)["badges"][0]["totals"]["throw"], 2500)
        restarted.trim(now=t0 + 3700)
        self.assertEqual(restarted.history["c03cfd52"], [[ms(200), 2500, 1], [ms(202), 2500]])

    def test_rescan_when_a_recent_badge_goes_quiet(self):
        self.dash.observe("A", "b1", -50, mfg(3), stats(3, 1000), now=100.0)
        self.assertFalse(self.dash.needs_rescan(now=105.0))
        self.assertTrue(self.dash.needs_rescan(now=110.0))
        self.assertFalse(self.dash.needs_rescan(now=2000.0))  # long gone

    def test_reset_all_clears_history(self):
        t0 = float(int(time.time()) - 100)
        self.dash.observe("A", "b1", -50, mfg(3), stats(3, 1000, 800), now=t0)
        self.dash.sample(now=t0)
        self.dash.reset_all(now=t0 + 10)
        self.assertEqual(self.dash.history, {})
        self.dash.sample(now=t0 + 10)  # the advert heard before the reset isn't charted
        self.assertEqual(self.dash.history.get("c03cfd52", []), [])
        restarted = Dashboard(self.state_file)
        restarted.observe("A", "b1", -50, mfg(3), stats(3, 1000, 900), now=t0 + 20)
        restarted.sample(now=t0 + 20)
        self.assertEqual(restarted.history["c03cfd52"], [[int((t0 + 20) * 1000), 100]])

    def test_offline_and_old_build(self):
        self.dash.observe("A", None, -60, mfg(1, state=3), None, now=80.0)
        badge = self.dash.snapshot(now=100.0)["badges"][0]
        self.assertFalse(badge["online"])
        self.assertIsNone(badge["totals"])
        self.assertEqual(badge["state"], "throw")
        self.assertFalse(self.dash.reset("c03cfd52"))


if __name__ == "__main__":
    unittest.main()
