"""Badge-side tooling: protocol mirror and the autostart boot-script patch."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import deploy_badge  # noqa: E402

# The shape of the badge's stock /system/main.py around the lines we patch.
STOCK_MAIN = '''import machine
import sys

MENU_DIR = "/system/apps/menu"
preloaded = []

try:
    import sparkle
except Exception as error:  # noqa: BLE001
    sparkle = None

badge.poll()

app_to_launch = launch("/system/apps/menu")
'''


class ProtocolMirrorTests(unittest.TestCase):
    def test_laptop_and_badge_protocol_are_identical(self):
        laptop = (ROOT / "protocol.py").read_bytes()
        badge = (ROOT / "badge" / "apps" / "airtime" / "protocol.py").read_bytes()
        self.assertEqual(laptop.replace(b"\r\n", b"\n"), badge.replace(b"\r\n", b"\n"),
                         "protocol.py and badge/apps/airtime/protocol.py must stay identical")


class AutostartPatchTests(unittest.TestCase):
    def test_patch_adds_autostart_and_keeps_menu_fallback(self):
        patched = deploy_badge.patch_main(STOCK_MAIN)
        self.assertIn("import machine\nimport os\n", patched)
        self.assertIn('open("/state/autostart")', patched)
        self.assertIn("machine.WDT_RESET", patched)
        self.assertIn('app_to_launch = autostart or launch("/system/apps/menu")', patched)
        self.assertLess(patched.index("autostart = None"), patched.index("import sparkle"))
        compile(patched, "main.py", "exec")

    def test_patch_is_idempotent(self):
        once = deploy_badge.patch_main(STOCK_MAIN)
        self.assertEqual(deploy_badge.patch_main(once), once)

    def test_unknown_boot_script_is_refused(self):
        with self.assertRaises(ValueError):
            deploy_badge.patch_main("print('hello')\n")


if __name__ == "__main__":
    unittest.main()
