"""Model upload over the control characteristic, against a fake badge."""
import asyncio
import unittest

import protocol
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
