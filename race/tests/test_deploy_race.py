"""Wi-Fi settings for deploy_race: wifi.json, env vars and flags."""
import json
import os
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import deploy_race  # noqa: E402


def args(ssid=None, password=None, server=None):
    return Namespace(wifi_ssid=ssid, wifi_password=password, server=server)


class NetSettingsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.file = Path(self.tmp.name) / "wifi.json"
        env = mock.patch.dict(os.environ, {}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("AIRTIME_WIFI_SSID", None)
        os.environ.pop("AIRTIME_WIFI_PASSWORD", None)

    def write(self, data):
        self.file.write_text(json.dumps(data), encoding="utf-8")

    def test_missing_file_sends_nothing(self):
        self.assertEqual(deploy_race.net_settings(args(), self.file), {})

    def test_reads_wifi_json(self):
        self.write({"ssid": "home", "password": "pw", "server": "10.0.0.2"})
        self.assertEqual(deploy_race.net_settings(args(), self.file),
                         {"ssid": "home", "password": "pw", "server": "10.0.0.2"})

    def test_env_and_flags_override_file(self):
        self.write({"ssid": "home", "password": "pw", "server": "10.0.0.2"})
        os.environ["AIRTIME_WIFI_SSID"] = "env"
        os.environ["AIRTIME_WIFI_PASSWORD"] = "envpw"
        self.assertEqual(deploy_race.net_settings(args(password="flagpw", server="none"), self.file),
                         {"ssid": "env", "password": "flagpw", "server": ""})

    def test_invalid_json_exits(self):
        self.file.write_text("{", encoding="utf-8")
        with self.assertRaises(SystemExit):
            deploy_race.net_settings(args(), self.file)


if __name__ == "__main__":
    unittest.main()
