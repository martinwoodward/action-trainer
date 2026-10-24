"""Wire formats, the leaderboard's UDP link and its aggregation of per-state totals."""
import asyncio
import tempfile
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

    def test_lifetime_totals_do_not_wrap(self):
        big = 5 * 2**32 + 7  # far past uint32 ms (49.7 days)
        packet = protocol.pack_status(1, 300, 1, 0, 0, big, big, 70000, 2**31, 2**20,
                                      big, big + 1, big + 2, -40, "x")
        st = protocol.unpack_status(packet)
        self.assertEqual((st["still_ms"], st["throw_ms"], st["drop_ms"], st["air_total_ms"]),
                         (big, big + 1, big + 2, big))
        self.assertEqual((st["boot"], st["drops"], st["throws"], st["steps"]), (300, 70000, 2**31, 2**20))

    def test_ack_carries_the_reset_offset(self):
        kind, badge_id, payload = protocol.unpack_payload(protocol.pack_ack(9, 2**33))
        self.assertEqual((kind, badge_id, protocol.unpack_ack(payload)), (protocol.PKT_ACK, 9, 2**33))
        self.assertEqual(protocol.unpack_ack(b""), 0)

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

    def test_ping_is_always_acked_at_once(self):
        addr = ("192.168.9.211", 47217)
        self.link.datagram_received(status_packet(), addr)  # acked, starts the ACK_EVERY_S timer
        self.link.datagram_received(protocol.pack_ping(0xC03CFD52), addr)
        self.link.datagram_received(protocol.pack_ping(0xC03CFD52), addr)
        self.assertEqual(self.transport.sent, [(protocol.pack_ack(0xC03CFD52), addr)] * 3)

    def test_acks_carry_the_offset_after_a_reset(self):
        addr = ("192.168.9.208", 47217)
        self.assertFalse(self.link.ack("c03cfd52"))  # never heard
        self.link.datagram_received(status_packet(throw=600), addr)
        self.dash.reset("c03cfd52")
        self.assertTrue(self.link.ack("c03cfd52"))
        self.assertEqual(self.transport.sent[-1], (protocol.pack_ack(0xC03CFD52, 600), addr))

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

    def test_reboot_keeps_page_reset_and_lifetime(self):
        self.dash.observe("A", "b1", -50, mfg(3), stats(3, 10000, 500), now=100.0)
        self.dash.reset("c03cfd52")
        self.dash.observe("A", "b1", -50, mfg(4), stats(4, 10300, 900), now=100.0)  # rebooted; totals kept growing
        self.assertEqual(self.totals(), {"still": 300, "throw": 400, "drop": 0})
        self.assertEqual(self.dash.snapshot(now=100.0)["badges"][0]["lifetime"]["throw"], 900)
        self.assertIn("c03cfd52", Leaderboard(self.state_file).baselines)

    def test_power_cut_rollback_is_clamped(self):
        self.dash.observe("A", "b1", -50, mfg(3), stats(3, 10000, 500), now=100.0)
        self.dash.reset("c03cfd52")
        self.dash.observe("A", "b1", -50, mfg(4), stats(4, 9500, 500), now=100.0)  # lost unsaved still time
        self.assertEqual(self.totals(), {"still": 0, "throw": 0, "drop": 0})

    def test_old_per_boot_baselines_are_dropped(self):
        self.state_file.write_text('{"baselines": {"a": {"boot": 3, "ms": {"still": 1, "throw": 2, "drop": 3}},'
                                   ' "b": {"ms": {"still": 1, "throw": 2, "drop": 3}}}}', encoding="utf-8")
        self.assertEqual(list(Leaderboard(self.state_file).baselines), ["b"])

    def test_legacy_cached_totals_are_not_used_as_offsets(self):
        self.state_file.write_text('{"badges": {"c03cfd52": {"badge_id": "c03cfd52", "name": "b1", "boot": 3,'
                                   ' "totals": {"still": 1, "throw": 60000, "drop": 0}, "seen": 0, "rssi": -50,'
                                   ' "state": "still"}}}', encoding="utf-8")
        dash = Leaderboard(self.state_file)
        self.assertFalse(dash.reset("c03cfd52"))  # nothing to reset until a v3 report arrives
        dash.observe("A", "b1", -50, mfg(1), stats(1, 1000, 2000), now=100.0)
        self.assertEqual(dash.snapshot(now=100.0)["badges"][0]["totals"]["throw"], 2000)

    def test_reset_all_zeros_every_badge_and_persists(self):
        self.dash.observe("A", "b1", -50, mfg(3), stats(3, 1000, 800), now=100.0)
        self.dash.reset_all()
        self.dash.observe("A", "b1", -50, mfg(3), stats(3, 1000, 900), now=101.0)
        self.assertEqual(self.totals(), {"still": 0, "throw": 100, "drop": 0})
        restarted = Leaderboard(self.state_file)
        restarted.observe("A", "b1", -50, mfg(4), stats(4, 1000, 950), now=101.0)
        self.assertEqual(restarted.snapshot(now=101.0)["badges"][0]["totals"]["throw"], 150)

    def test_offline_and_old_build(self):
        self.dash.observe("A", None, -60, mfg(1, state=3), None, now=80.0)
        badge = self.dash.snapshot(now=100.0)["badges"][0]
        self.assertFalse(badge["online"])
        self.assertIsNone(badge["totals"])
        self.assertEqual(badge["state"], "throw")
        self.assertFalse(self.dash.reset("c03cfd52"))


if __name__ == "__main__":
    unittest.main()
