# Kiosk boot: every boot (including the reset switch) goes straight into the
# Airtime app. No startup animation, no menu, and no way out of the app.
#
# Escape routes:
# - Double-tap reset: USB mass storage (same as upstream badger/home main.py).
# - USB serial: mpremote can interrupt the app at any time.
# Restore the stock boot with `tools/deploy_badge.py restore`.
import os
import powman

APP_DIRS = ("/state/apps/airtime", "/system/apps/airtime")

if powman.get_wake_reason() == powman.WAKE_DOUBLETAP:
    import _msc


def _app_dir():
    for path in APP_DIRS:
        try:
            os.stat(path + "/__init__.py")
            return path
        except OSError:
            pass
    return None


# Eat the wakeup button press so it doesn't leak into the app
badge.poll()
while badge.pressed() or badge.held() or badge.released():
    badge.poll()

app = _app_dir()
if app is None:
    print("kiosk: no airtime app in", APP_DIRS)
    import time
    time.sleep(5)  # leave a window for serial to attach before rebooting
else:
    launch(app)

# The app never exits by itself; if it returns, reboot back into it
reset()
