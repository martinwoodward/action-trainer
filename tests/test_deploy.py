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


if __name__ == "__main__":
    unittest.main()
