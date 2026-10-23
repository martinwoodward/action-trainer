"""Model upload over the control characteristic, against a fake badge."""
import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import protocol
import recorder
from recorder import Recorder


class FakeBadge:
    """Mirrors the pedometer app's upload handling."""

    mtu_size = 247

    def __init__(self):
        self.chunks = []
        self.tag = "no"

    async def write_gatt_char(self, _uuid, data, response=True):
        assert len(data) <= protocol.CONTROL_MAX
        op, arg = data[:1], data[1:]
        if op == protocol.CMD_MODEL_BEGIN:
            self.chunks = []
        elif op == protocol.CMD_MODEL_DATA:
            self.chunks.append(arg)
        elif op == protocol.CMD_MODEL_END:
            length, checksum = (int(v) for v in arg.decode().split(","))
            src = b"".join(self.chunks)
            ok = len(src) == length and protocol.model_checksum(src) == checksum
            self.tag = "%04x" % checksum if ok else "err"

    async def read_gatt_char(self, _uuid):
        return f"pedometer v1 model={self.tag}".encode()


class DeployTests(unittest.TestCase):
    def test_upload_round_trip(self):
        rec, badge = Recorder(), FakeBadge()
        rec.client = badge
        source = bytes(range(256)) * 13
        tag = asyncio.run(rec.deploy_model(source))
        self.assertEqual(b"".join(badge.chunks), source)
        self.assertEqual(tag, "%04x" % protocol.model_checksum(source))

    def test_requires_connection(self):
        with self.assertRaises(RuntimeError):
            asyncio.run(Recorder().deploy_model(b"x"))


class GateTests(unittest.TestCase):
    """Train & deploy keeps the badge's model when the new one scores worse."""

    def run_round(self, rec, dist, new_acc, baseline_acc, force=False):
        def fake_train(_data, output, baseline):
            output.write_text(f"# model {new_acc}")
            test = {"accuracy": new_acc, "recall": {"walk": new_acc}}
            if baseline is not None and baseline_acc is not None:
                test["baseline"] = {"accuracy": baseline_acc, "recall": {"walk": baseline_acc}}
            return {"test": test, "windows_per_label": {"walk": 10}}

        sent = []

        async def broadcast(msg):
            sent.append(msg)

        rec.broadcast = broadcast
        with mock.patch.multiple(recorder, MODEL_PATH=dist / "model.py",
                                 DEPLOYED_MODEL=dist / "deployed_model.py",
                                 DEPLOYED_CARD=dist / "deployed.json",
                                 MODEL_HIDDEN=dist / "model_hidden"), \
                mock.patch.object(recorder.trainer, "train", fake_train):
            asyncio.run(rec.train_and_deploy(force))
            return sent[-1], rec.model_card()

    def test_hidden_demo_model_is_always_replaced_without_comparison(self):
        with tempfile.TemporaryDirectory() as temp:
            dist = Path(temp)
            rec, badge = Recorder(), FakeBadge()
            rec.client = badge
            rec.hide_model = False
            self.run_round(rec, dist, 0.9, None)
            rec.hide_model = True
            (dist / "model_hidden").touch()

            msg, card = self.run_round(rec, dist, 0.5, 0.9)
            self.assertIsNotNone(msg["deployed"])
            self.assertIsNone(card["kept"])
            self.assertIsNone(card["deployed"]["previous"])
            self.assertFalse(card["hidden"])
            self.assertFalse((dist / "model_hidden").exists())
            self.assertEqual((dist / "deployed_model.py").read_text(), "# model 0.5")

    def test_worse_model_is_kept_back_unless_forced(self):
        with tempfile.TemporaryDirectory() as temp:
            dist = Path(temp)
            rec, badge = Recorder(), FakeBadge()
            rec.client = badge
            rec.hide_model = False
            msg, card = self.run_round(rec, dist, 0.9, None)
            self.assertIsNotNone(msg["deployed"])
            self.assertEqual(card["deployed"]["test"]["accuracy"], 0.9)
            self.assertEqual((dist / "deployed_model.py").read_text(), "# model 0.9")

            msg, card = self.run_round(rec, dist, 0.8, 0.9)
            self.assertIsNone(msg["deployed"])
            self.assertEqual(card["kept"]["test"]["accuracy"], 0.8)
            self.assertEqual((dist / "deployed_model.py").read_text(), "# model 0.9")

            msg, card = self.run_round(rec, dist, 0.8, 0.9, force=True)
            self.assertIsNotNone(msg["deployed"])
            self.assertIsNone(card["kept"])
            self.assertEqual(card["deployed"]["previous"]["accuracy"], 0.9)
            self.assertEqual((dist / "deployed_model.py").read_text(), "# model 0.8")

            # A badge running some other model gets no gate and no stale scorecard.
            rec.info = "pedometer v1 id=2 model=beef name=OTHER"
            self.assertIsNone(rec.model_card()["deployed"])
            msg, card = self.run_round(rec, dist, 0.5, 0.9)
            self.assertIsNotNone(msg["deployed"])
            self.assertEqual((dist / "deployed_model.py").read_text(), "# model 0.5")


class DeadBadge:
    async def write_gatt_char(self, *_args, **_kwargs):
        raise OSError("gone")

    async def disconnect(self):
        pass


class KeepAliveTests(unittest.TestCase):
    def test_link_dropped_after_repeated_ping_failures(self):
        async def run():
            rec = Recorder()
            rec.loop = asyncio.get_running_loop()
            rec.client, rec.target, rec.ping_failures = DeadBadge(), "addr", 0
            await rec.keep_alive()
            self.assertIsNotNone(rec.client)
            await rec.keep_alive()
            self.assertIsNone(rec.client)
            self.assertEqual(rec.status()["reconnecting"], "addr")

        asyncio.run(run())

    def test_reconnects_when_target_is_seen(self):
        async def run():
            rec = Recorder()
            rec.target = "addr"
            rec.badges["addr"] = {"seen": __import__("time").time()}
            calls = []

            async def fake_connect(address):
                calls.append(address)

            rec.connect = fake_connect
            await rec.keep_alive()
            self.assertEqual(calls, ["addr"])

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
