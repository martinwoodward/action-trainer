"""Measure how fast badges report to the dashboard over Wi-Fi (UDP).

Listens where the dashboard does (stop the dashboard first), acks badges like
the dashboard, and pings each badge. For each badge it reports:
- gaps between status datagrams (p50/p90/max) and loss from sequence numbers;
- ping round-trip time (p50/p90/max), an upper bound on one-way latency;
- how many state changes arrived.

    python tools/udp_latency.py [--seconds 60] [--ping-every 0.5] [--json out.json]
"""
import argparse
import asyncio
import json
import os
import statistics
import struct
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import protocol  # noqa: E402


def pct(values, q):
    if not values:
        return None
    values = sorted(values)
    return values[min(len(values) - 1, int(round(q * (len(values) - 1))))]


def summary(values):
    if not values:
        return {"n": 0}
    return {"n": len(values), "p50": round(pct(values, 0.5), 1), "p90": round(pct(values, 0.9), 1),
            "max": round(max(values), 1), "mean": round(statistics.mean(values), 1)}


class Probe(asyncio.DatagramProtocol):
    def __init__(self):
        self.transport = None
        self.badges = {}  # badge_id -> stats

    def connection_made(self, transport):
        self.transport = transport

    def badge(self, badge_id):
        return self.badges.setdefault(badge_id, {
            "name": None, "addr": None, "packets": 0, "lost": 0, "last_seq": None, "last_t": None,
            "gaps_ms": [], "rtt_ms": [], "pings": 0, "changes": 0, "state": None, "acked": 0,
        })

    def datagram_received(self, data, addr):
        now = time.perf_counter()
        msg = protocol.unpack_payload(data)
        if msg is None:
            return
        kind, badge_id, payload = msg
        b = self.badge(badge_id)
        if kind == protocol.PKT_STATUS:
            st = protocol.unpack_status(data)
            if st is None:
                return
            b["name"], b["addr"] = st["name"], addr
            b["packets"] += 1
            if b["last_seq"] is not None:
                b["lost"] += max(0, ((st["seq"] - b["last_seq"]) & 0xFFFF) - 1)
                b["gaps_ms"].append((now - b["last_t"]) * 1000)
            if b["state"] is not None and st["state"] != b["state"]:
                b["changes"] += 1
            b["last_seq"], b["last_t"], b["state"] = st["seq"], now, st["state"]
            if time.time() - b["acked"] >= 2:
                b["acked"] = time.time()
                self.transport.sendto(protocol.pack_ack(badge_id), addr)
        elif kind == protocol.PKT_REPLY and len(payload) == 8:
            (sent,) = struct.unpack("<d", payload)
            b["rtt_ms"].append((now - sent) * 1000)

    def ping_all(self):
        for badge_id, b in self.badges.items():
            if b["addr"]:
                b["pings"] += 1
                token = struct.pack("<d", time.perf_counter())
                self.transport.sendto(protocol.pack_cmd(badge_id, protocol.CMD_PING, token), b["addr"])


async def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--seconds", type=float, default=60)
    parser.add_argument("--ping-every", type=float, default=0.5)
    parser.add_argument("--udp-port", type=int, default=protocol.UDP_PORT)
    parser.add_argument("--json", help="also write the results here")
    args = parser.parse_args()

    probe = Probe()
    loop = asyncio.get_running_loop()
    transport, _ = await loop.create_datagram_endpoint(lambda: probe, local_addr=("0.0.0.0", args.udp_port))
    print(f"listening on UDP {args.udp_port} for {args.seconds:.0f} s…", flush=True)
    end = time.time() + args.seconds
    while time.time() < end:
        await asyncio.sleep(args.ping_every)
        probe.ping_all()
    await asyncio.sleep(0.5)  # let the last replies in
    transport.close()

    results = {}
    for badge_id, b in sorted(probe.badges.items(), key=lambda kv: kv[1]["name"] or ""):
        sent = b["packets"] + b["lost"]
        results["%08x" % badge_id] = {
            "name": b["name"],
            "packets": b["packets"],
            "loss_pct": round(100 * b["lost"] / sent, 2) if sent else None,
            "gap_ms": summary(b["gaps_ms"]),
            "rtt_ms": summary(b["rtt_ms"]),
            "pings": b["pings"],
            "ping_loss_pct": round(100 * (1 - len(b["rtt_ms"]) / b["pings"]), 2) if b["pings"] else None,
            "state_changes": b["changes"],
        }
    if not results:
        print("No badges heard. Are they on the same network, and is the dashboard stopped?")
    for badge_id, r in results.items():
        print(f"{r['name'] or badge_id} ({badge_id}): {r['packets']} packets, loss {r['loss_pct']}%, "
              f"gap {r['gap_ms']}, rtt {r['rtt_ms']} (ping loss {r['ping_loss_pct']}%), "
              f"{r['state_changes']} state changes")
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(results, f, indent=2)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
