"""Air-time advertising payload, shared by the badge and the laptop tools.

Legacy advertising leaves 31 bytes; flags (3) + manufacturer AD header (2)
+ this 26-byte body fills it exactly. The badge name goes in the scan
response. Counters only increase (u8 counters wrap at 256), so listeners can
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


def scan_response(name):
    raw = name.encode()[:29]
    return bytes((len(raw) + 1, 0x09)) + raw


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
CMD_PING = b"P"  # P: heartbeat; the badge drops links that go quiet
PING_INTERVAL_MS = 2000
LINK_TIMEOUT_MS = 8000
# Model upload: B, then D chunks of model.py source, then E<length>,<checksum>.
# The badge saves it to /state and loads it; INFO then reports model=<checksum>.
CMD_MODEL_BEGIN = b"B"
CMD_MODEL_DATA = b"D"
CMD_MODEL_END = b"E"
CMD_MODEL_SHOW = b"M"  # M1 shows / M0 hides the model line on the badge (demo); persisted, upload re-shows
CONTROL_MAX = 244  # control characteristic buffer on the badge


def model_checksum(data):
    return sum(data) & 0xFFFF


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
