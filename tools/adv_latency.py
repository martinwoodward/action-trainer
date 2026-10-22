"""Measure how promptly this computer hears each badge's adverts.

Scans passively (like the dashboard) for a fixed time and prints, per badge:

- gap: time between advert callbacks, and between adverts with a new payload
  (the badge's advert sequence number changed);
- skipped: payload updates the badge made that we never heard (seq jumps);
- flights: take-offs heard while the badge still said "throw", and flights
  only noticed afterwards because its throw_ms counter had grown.

Run it with throws going on to see the take-off lag:

    python tools/adv_latency.py --seconds 120 [--json out.json]
"""

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bleak import BleakScanner  # noqa: E402

from dashboard import STATE_BY_CODE, decode  # noqa: E402


def pct(values, q):
    if not values:
        return None
    values = sorted(values)
    return values[min(len(values) - 1, int(q * len(values)))]


class Badge:
    def __init__(self, name):
        self.name = name
        self.times = []        # every callback
        self.fresh = []        # callbacks with a new seq
        self.seq = None
        self.skipped = 0
        self.updates = 0
        self.throw_ms = None
        self.flying = False
        self.heard_in_air = 0  # take-offs heard while the badge said "throw"
        self.missed = 0        # flights only noticed afterwards, from throw_ms
        self.unclaimed = False # a heard take-off whose air time hasn't been seen yet
        self.flew_at = 0.0

    def add(self, now, mfg, stats, name):
        self.name = name or self.name
        self.times.append(now)
        if mfg["seq"] != self.seq:
            if self.seq is not None:
                step = (mfg["seq"] - self.seq) & 0xFF
                self.updates += step
                self.skipped += step - 1
            self.seq = mfg["seq"]
            self.fresh.append(now)
        flying = STATE_BY_CODE.get(mfg["state"]) == "throw"
        if flying and not self.flying:
            self.heard_in_air += 1
            self.unclaimed = True
        if stats is not None and stats["boot"] == mfg["boot"]:
            # The totals ride in the scan response, which can lag the state, so a
            # rise only counts as missed if no throw state was heard around it.
            air = stats["throw_ms"]
            if self.throw_ms is not None and air > self.throw_ms and not flying:
                # A rise soon after a heard flight is that flight's lagging total.
                if not self.unclaimed and now - self.flew_at > 1.5:
                    self.missed += 1
                self.unclaimed = False
                self.flew_at = now
            self.throw_ms = air
        if flying:
            self.flew_at = now
        self.flying = flying

    def summary(self):
        gaps = [b - a for a, b in zip(self.times, self.times[1:])]
        fresh = [b - a for a, b in zip(self.fresh, self.fresh[1:])]
        r = lambda v: None if v is None else round(v, 2)  # noqa: E731
        return {
            "name": self.name,
            "callbacks": len(self.times),
            "gap_p50_s": r(pct(gaps, 0.5)), "gap_p90_s": r(pct(gaps, 0.9)), "gap_max_s": r(max(gaps, default=None)),
            "fresh_p50_s": r(pct(fresh, 0.5)), "fresh_p90_s": r(pct(fresh, 0.9)),
            "fresh_max_s": r(max(fresh, default=None)),
            "updates_heard_pct": round(100 * (self.updates - self.skipped) / self.updates) if self.updates else None,
            "heard_in_air": self.heard_in_air,
            "missed_in_air": self.missed,
        }


async def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--seconds", type=float, default=60)
    ap.add_argument("--json", type=Path, help="also write the summary here")
    args = ap.parse_args()

    badges = {}

    def on_adv(dev, adv):
        mfg, stats = decode(adv)
        if mfg is None:
            return
        key = "%08x" % mfg["badge_id"]
        badges.setdefault(key, Badge(key)).add(time.monotonic(), mfg, stats, adv.local_name)

    async with BleakScanner(on_adv):
        print(f"scanning for {args.seconds:g} s…", flush=True)
        await asyncio.sleep(args.seconds)

    out = {k: b.summary() for k, b in sorted(badges.items(), key=lambda kv: kv[1].name)}
    cols = ["callbacks", "gap_p50_s", "gap_p90_s", "gap_max_s", "fresh_p50_s", "fresh_p90_s", "fresh_max_s",
            "updates_heard_pct", "heard_in_air", "missed_in_air"]
    print("badge".ljust(12) + "".join(c.replace("_", " ")[:12].rjust(13) for c in cols))
    for s in out.values():
        print(s["name"][:12].ljust(12) + "".join(str(s[c]).rjust(13) for c in cols))
    if args.json:
        args.json.write_text(json.dumps(out, indent=2), encoding="utf-8")


if __name__ == "__main__":
    asyncio.run(main())
