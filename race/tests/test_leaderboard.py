"""Wire formats, the leaderboard's UDP link and its aggregation of per-state totals."""
import asyncio
import tempfile
import time
import unittest
from pathlib import Path

import protocol
from leaderboard import BadgeLink, Leaderboard


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


def status_packet(boot=3, state=0, still=1000, throw=0, drop=0, name="Copilot", seq=1):
    return protocol.pack_status(0xC03CFD52, boot, seq, state, 120, 5000, 4000, 2, 7, 300,
                                still, throw, drop, -54, name)


class UdpFormatTests(unittest.TestCase):
    def test_status_round_trips(self):
        st = protocol.unpack_status(status_packet(state=3, still=123456, throw=789, drop=42, seq=70000))
        self.assertEqual(st["badge_id"], 0xC03CFD52)
        self.assertEqual((st["boot"], st["seq"], st["state"]), (3, 70000 & 0xFFFF, 3))
        self.assertEqual((st["still_ms"], st["throw_ms"], st["drop_ms"]), (123456, 789, 42))
        self.assertEqual((st["air_total_ms"], st["throws"], st["rssi"], st["name"]), (5000, 7, -54, "Copilot"))

    def test_name_is_truncated_and_bad_packets_rejected(self):
        st = protocol.unpack_status(status_packet(name="a-very-long-badge-name"))
        self.assertEqual(st["name"], "a-very-long-badg")
        self.assertIsNone(protocol.unpack_status(status_packet()[:-10]))
        self.assertIsNone(protocol.unpack_status(b"XX" + status_packet()[2:]))
        self.assertIsNone(protocol.unpack_status(protocol.pack_ack(1)))

    def test_command_round_trips(self):
        kind, badge_id, payload = protocol.unpack_payload(protocol.pack_cmd(0xC03CFD52, protocol.CMD_NAME, b"Mona"))
        self.assertEqual((kind, badge_id, payload), (protocol.PKT_CMD, 0xC03CFD52, b"NMona"))


class FakeTransport:
    def __init__(self):
        self.sent = []

    def sendto(self, data, addr):
        self.sent.append((data, addr))


class BadgeLinkTests(unittest.TestCase):
    def setUp(self):
        self.dash = Leaderboard(None)
        self.heard = asyncio.Event()
        self.replies = []
        self.link = BadgeLink(self.dash, self.heard, lambda b, p: self.replies.append((b, p)))
        self.transport = FakeTransport()
        self.link.connection_made(self.transport)

    def test_status_updates_leaderboard_and_is_acked_once(self):
        addr = ("192.168.9.208", 47217)
        self.link.datagram_received(status_packet(state=3, still=900, throw=600), addr)
        self.link.datagram_received(status_packet(state=3, still=900, throw=650, seq=2), addr)
        badge = self.dash.snapshot()["badges"][0]
        self.assertEqual((badge["name"], badge["state"], badge["rssi"]), ("Copilot", "throw", -54))
        self.assertEqual(badge["totals"], {"still": 900, "throw": 650, "drop": 0})
        self.assertEqual(badge["drops"], 2)  # the page plays UGF when this changes
        self.assertTrue(self.heard.is_set())
        self.assertEqual(self.transport.sent, [(protocol.pack_ack(0xC03CFD52), addr)])  # once per ACK_EVERY_S

    def test_commands_go_to_the_last_address_and_replies_are_routed(self):
        self.assertFalse(self.link.command("c03cfd52", protocol.CMD_IDENTIFY))  # never heard
        self.link.datagram_received(status_packet(), ("10.0.0.5", 5000))
        self.assertTrue(self.link.command("c03cfd52", protocol.CMD_NAME, b"Duckie"))
        self.assertEqual(self.transport.sent[-1], (protocol.pack_cmd(0xC03CFD52, b"N", b"Duckie"), ("10.0.0.5", 5000)))
        self.link.datagram_received(protocol.pack_reply(0xC03CFD52, b"tok"), ("10.0.0.5", 5000))
        self.assertEqual(self.replies, [("c03cfd52", b"tok")])
        self.link.datagram_received(b"junk", ("10.0.0.6", 1))  # ignored


class LeaderboardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state_file = Path(self.temp.name) / "state.json"
        self.dash = Leaderboard(self.state_file)

    def tearDown(self):
        self.temp.cleanup()

    def totals(self):
        return self.dash.snapshot(now=100.0)["badges"][0]["totals"]

    def test_page_reset_subtracts_and_survives_restart(self):
        self.dash.observe("A", "b1", -50, mfg(3), stats(3, 10000, 500), now=100.0)
        self.assertTrue(self.dash.reset("c03cfd52"))
        self.dash.observe("A", "b1", -50, mfg(3, state=4), stats(3, 12000, 1500, 2000), now=100.0)
        self.assertEqual(self.totals(), {"still": 2000, "throw": 1000, "drop": 2000})

        restarted = Leaderboard(self.state_file)
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
        self.assertNotIn("c03cfd52", Leaderboard(self.state_file).baselines)

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
        restarted = Leaderboard(self.state_file)
        self.assertEqual(restarted.history, self.dash.history)
        self.assertEqual(restarted.snapshot(now=t0 + 202)["badges"][0]["totals"]["throw"], 2500)
        restarted.trim(now=t0 + 3700)
        self.assertEqual(restarted.history["c03cfd52"], [[ms(200), 2500, 1], [ms(202), 2500]])

    def test_reset_all_clears_history(self):
        t0 = float(int(time.time()) - 100)
        self.dash.observe("A", "b1", -50, mfg(3), stats(3, 1000, 800), now=t0)
        self.dash.sample(now=t0)
        self.dash.reset_all(now=t0 + 10)
        self.assertEqual(self.dash.history, {})
        self.dash.sample(now=t0 + 10)  # the advert heard before the reset isn't charted
        self.assertEqual(self.dash.history.get("c03cfd52", []), [])
        restarted = Leaderboard(self.state_file)
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
