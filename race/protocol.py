"""Air-time wire formats, shared by the badge and the laptop tools.

The Race app reports to the leaderboard over Wi-Fi (UDP, see the end of this
file). The BLE formats below remain for the recorder and for older builds.

Legacy advertising leaves 31 bytes; flags (3) + manufacturer AD header (2)
+ this 26-byte body fills it exactly. The badge name goes in the scan
response, together with per-state time totals (service data, see
pack_stats). Counters only increase (u8 counters wrap at 256), so listeners can
miss packets without losing events.
"""

import struct

COMPANY_ID = 0xFFFF
MAGIC = b"AT"
VERSION = 1
MFG_FORMAT = "<H2sBIBBBHIIBBH"
MFG_SIZE = struct.calcsize(MFG_FORMAT)

FIELDS = (
    "company",
    "magic",
    "ver",
    "badge_id",
    "boot",
    "seq",
    "state",
    "flight_ms",
    "air_total_ms",
    "air_at_reset_ms",
    "drops",
    "throws",
    "steps",
)


def pack_mfg(badge_id, boot, seq, state, flight_ms, air_total_ms, air_at_reset_ms, drops, throws, steps):
    return struct.pack(
        MFG_FORMAT,
        COMPANY_ID,
        MAGIC,
        VERSION,
        badge_id & 0xFFFFFFFF,
        boot & 0xFF,
        seq & 0xFF,
        state & 0xFF,
        min(flight_ms, 0xFFFF),
        air_total_ms & 0xFFFFFFFF,
        air_at_reset_ms & 0xFFFFFFFF,
        drops & 0xFF,
        throws & 0xFF,
        steps & 0xFFFF,
    )


def adv_payload(mfg):
    return bytes((2, 0x01, 0x06, len(mfg) + 1, 0xFF)) + mfg


# Scan response service data (AD 0x16): ms spent in each model state since
# boot. It is service data rather than a second 0xFFFF manufacturer block so
# that scanners which merge advert and scan response keep both.
STATS_UUID16 = 0xA17E
STATS_UUID = "0000a17e-0000-1000-8000-00805f9b34fb"
STATS_FORMAT = "<HBIII"
STATS_BODY_FORMAT = "<BIII"  # as delivered by scanners, after the UUID
STATS_FIELDS = ("boot", "still_ms", "throw_ms", "drop_ms")
NAME_MAX = 31 - 2 - struct.calcsize(STATS_FORMAT) - 2  # 12 bytes


def pack_stats(boot, still_ms, throw_ms, drop_ms):
    return struct.pack(
        STATS_FORMAT,
        STATS_UUID16,
        boot & 0xFF,
        still_ms & 0xFFFFFFFF,
        throw_ms & 0xFFFFFFFF,
        drop_ms & 0xFFFFFFFF,
    )


def unpack_stats(data):
    """Decode stats service data *without* the UUID (bleak's service_data value)."""
    if data is None or len(data) < struct.calcsize(STATS_BODY_FORMAT):
        return None
    return dict(zip(STATS_FIELDS, struct.unpack_from(STATS_BODY_FORMAT, data, 0)))


def scan_response(name, stats=None):
    raw = name.encode()[: NAME_MAX if stats else 29]
    out = bytes((len(raw) + 1, 0x09)) + raw
    if stats:
        out += bytes((len(stats) + 1, 0x16)) + stats
    return out


# --- GATT (recording and setup) ------------------------------------------
SERVICE_UUID = "b4d6e000-a1e5-4a1e-9b1e-000000000001"
STREAM_UUID = "b4d6e000-a1e5-4a1e-9b1e-000000000002"
CONTROL_UUID = "b4d6e000-a1e5-4a1e-9b1e-000000000003"
INFO_UUID = "b4d6e000-a1e5-4a1e-9b1e-000000000004"

# Stream notify: header (seq u16, count u8) then `count` samples of
# (ticks & 0xFFFF u16, ax ay az gx gy gz i16, imu event bits u8, detector state u8).
STREAM_HEADER = "<HB"
STREAM_HEADER_SIZE = struct.calcsize(STREAM_HEADER)
SAMPLE_FORMAT = "<H6hBB"
SAMPLE_SIZE = struct.calcsize(SAMPLE_FORMAT)

# Control writes: one ASCII opcode byte, then a UTF-8 argument.
CMD_NAME = b"N"  # N<name>: rename the badge and persist it
CMD_LABEL = b"L"  # L<text>: show a recording prompt on the badge ("" clears)
CMD_IDENTIFY = b"I"  # I: flash the screen


def pack_samples(seq, samples):
    out = bytearray(struct.pack(STREAM_HEADER, seq & 0xFFFF, len(samples)))
    for s in samples:
        out += s
    return bytes(out)


def pack_sample(ticks, ax, ay, az, gx, gy, gz, events, state):
    return struct.pack(SAMPLE_FORMAT, ticks & 0xFFFF, ax, ay, az, gx, gy, gz, events & 0xFF, state & 0xFF)


def unpack_samples(data):
    """Return (seq, [(t16, ax, ay, az, gx, gy, gz, events, state), ...])."""
    seq, count = struct.unpack_from(STREAM_HEADER, data, 0)
    out = []
    for i in range(count):
        off = STREAM_HEADER_SIZE + i * SAMPLE_SIZE
        if off + SAMPLE_SIZE > len(data):
            break
        out.append(struct.unpack_from(SAMPLE_FORMAT, data, off))
    return seq, out


def unpack_mfg(data):
    """Decode manufacturer data *including* the 2-byte company ID."""
    if len(data) < MFG_SIZE:
        return None
    values = struct.unpack(MFG_FORMAT, data[:MFG_SIZE])
    if values[0] != COMPANY_ID or values[1] != MAGIC:
        return None
    return dict(zip(FIELDS, values))


# --- Wi-Fi (UDP) ------------------------------------------------------------
# The Race badge app sends a status datagram to the leaderboard on every state
# change and as a heartbeat. The leaderboard answers with acks (so a badge that
# broadcasts learns the server's address) and commands. Every datagram starts
# with MAGIC, UDP_VERSION and a one-byte type.
UDP_PORT = 47217
UDP_VERSION = 3
UDP_HEADER = "<2sBBI"  # magic, version, type, badge_id
UDP_HEADER_SIZE = struct.calcsize(UDP_HEADER)
PKT_STATUS = 0x53  # "S" badge -> server
PKT_ACK = 0x41  # "A" server -> badge: "I heard you; send to me directly" + ACK_BODY
PKT_CMD = 0x43  # "C" server -> badge: opcode byte + argument (CMD_*)
PKT_REPLY = 0x52  # "R" badge -> server: echo of a CMD_PING argument
PKT_PING = 0x50  # "P" badge -> server: "still there?"; answered at once with PKT_ACK

CMD_PING = b"P"  # P<token>: badge echoes <token> in a PKT_REPLY

# Totals are lifetime values that only ever grow (the badge saves them across
# reboots): ms as uint64, counts as uint32, so they can't wrap in practice.
STATUS_BODY = "<HHBHQQIIIQQQb"
STATUS_FIELDS = (
    "boot",
    "seq",
    "state",
    "flight_ms",
    "air_total_ms",
    "air_at_reset_ms",
    "drops",
    "throws",
    "steps",
    "still_ms",
    "throw_ms",
    "drop_ms",
    "rssi",
)
STATUS_SIZE = UDP_HEADER_SIZE + struct.calcsize(STATUS_BODY)
UDP_NAME_MAX = 16


def _header(kind, badge_id):
    return struct.pack(UDP_HEADER, MAGIC, UDP_VERSION, kind, badge_id & 0xFFFFFFFF)


def unpack_header(data):
    """Return (type, badge_id) for a datagram of this protocol, else None."""
    if len(data) < UDP_HEADER_SIZE:
        return None
    magic, ver, kind, badge_id = struct.unpack_from(UDP_HEADER, data, 0)
    if magic != MAGIC or ver != UDP_VERSION:
        return None
    return kind, badge_id


def pack_status(badge_id, boot, seq, state, flight_ms, air_total_ms, air_at_reset_ms, drops, throws, steps,
                still_ms, throw_ms, drop_ms, rssi, name):
    body = struct.pack(
        STATUS_BODY,
        boot & 0xFFFF,
        seq & 0xFFFF,
        state & 0xFF,
        min(flight_ms, 0xFFFF),
        air_total_ms,
        air_at_reset_ms,
        drops,
        throws,
        steps,
        still_ms,
        throw_ms,
        drop_ms,
        max(-128, min(127, rssi)),
    )
    return _header(PKT_STATUS, badge_id) + body + name.encode()[:UDP_NAME_MAX]


def unpack_status(data):
    """Decode a PKT_STATUS datagram into a dict (with badge_id and name), else None."""
    head = unpack_header(data)
    if head is None or head[0] != PKT_STATUS or len(data) < STATUS_SIZE:
        return None
    out = dict(zip(STATUS_FIELDS, struct.unpack_from(STATUS_BODY, data, UDP_HEADER_SIZE)))
    out["badge_id"] = head[1]
    out["name"] = bytes(data[STATUS_SIZE:STATUS_SIZE + UDP_NAME_MAX]).decode("utf-8", "replace")
    return out


# Badge colours, from the GitHub brand palette (brand.github.com/foundations/color):
# the Green, Copilot Purple, Orange, Security Blue and Lime ramps. Hues are
# interleaved, mid tones first, so the first badges get the most distinct colours.
# The leaderboard gives each badge its own index (in the ack); the page and the
# badge screen both draw from this list.
PALETTE = (
    "#0FBF3E", "#8534F3", "#FE4C25", "#3094FF", "#DB9D00",
    "#08872B", "#43179E", "#C53211", "#0527FC", "#D67200",
    "#5FED83", "#B870FF", "#F08A3A", "#1A61FE", "#D8BD0E",
    "#0A241B", "#26115F", "#801E0F", "#212183", "#703100",
    "#8CF2A6", "#C898FD", "#F4A876", "#9EECFF", "#D3FA37",
)


ACK_BODY = "<Q"  # throw_ms at the badge's last leaderboard reset (0 = never reset)
# Optional byte after ACK_BODY: the badge's colour, an index into the shared
# palette that the leaderboard assigns so no two badges share one. Older
# badges ignore it; NO_COLOUR (or a short ack) keeps the badge's own choice.
ACK_COLOUR = "<B"
NO_COLOUR = 0xFF


def pack_ack(badge_id, offset_ms=0, colour=NO_COLOUR):
    return _header(PKT_ACK, badge_id) + struct.pack(ACK_BODY, offset_ms) + struct.pack(ACK_COLOUR, colour & 0xFF)


def unpack_ack(payload):
    """Return the reset offset from a PKT_ACK payload (0 if absent)."""
    if len(payload) < struct.calcsize(ACK_BODY):
        return 0
    return struct.unpack_from(ACK_BODY, payload, 0)[0]


def unpack_ack_colour(payload):
    """Return the palette index from a PKT_ACK payload, else None."""
    at = struct.calcsize(ACK_BODY)
    if len(payload) < at + 1:
        return None
    colour = struct.unpack_from(ACK_COLOUR, payload, at)[0]
    return None if colour == NO_COLOUR else colour


def pack_cmd(badge_id, op, arg=b""):
    return _header(PKT_CMD, badge_id) + op + arg


def pack_reply(badge_id, arg):
    return _header(PKT_REPLY, badge_id) + arg


def pack_ping(badge_id):
    return _header(PKT_PING, badge_id)


def unpack_payload(data):
    """Return (type, badge_id, payload bytes after the header), else None."""
    head = unpack_header(data)
    if head is None:
        return None
    return head[0], head[1], bytes(data[UDP_HEADER_SIZE:])
