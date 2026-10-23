"""BLE for the air-time app: live advertising plus a GATT service for setup and recording.

Advertising carries the live counters. A laptop can connect to stream raw IMU
samples (Record) and to send control commands (name, prompt label, identify).
BTstack stops advertising while a central is connected; we resume on disconnect.
"""

import time

import bluetooth
from micropython import const

from protocol import (
    CONTROL_MAX,
    CONTROL_UUID,
    INFO_UUID,
    LINK_TIMEOUT_MS,
    SERVICE_UUID,
    STREAM_UUID,
    adv_payload,
    pack_samples,
    scan_response,
)

# Windows only sees ~1.8 updates/s at 100 ms but ~6/s at 20 ms (Phase 0).
ADV_INTERVAL_US = 20_000
STREAM_BATCH = 8

_IRQ_CENTRAL_CONNECT = const(1)
_IRQ_CENTRAL_DISCONNECT = const(2)
_IRQ_GATTS_WRITE = const(3)
_IRQ_MTU_EXCHANGED = const(21)


class Radio:
    def __init__(self, name, info):
        self._ble = bluetooth.BLE()
        self._ble.active(True)
        self._ble.config(gap_name=name[:20])
        service = (
            bluetooth.UUID(SERVICE_UUID),
            (
                (bluetooth.UUID(STREAM_UUID), bluetooth.FLAG_NOTIFY | bluetooth.FLAG_READ),
                (bluetooth.UUID(CONTROL_UUID), bluetooth.FLAG_WRITE | bluetooth.FLAG_WRITE_NO_RESPONSE),
                (bluetooth.UUID(INFO_UUID), bluetooth.FLAG_READ),
            ),
        )
        ((self._h_stream, self._h_control, self._h_info),) = self._ble.gatts_register_services((service,))
        self._ble.gatts_set_buffer(self._h_stream, 256)
        self._ble.gatts_set_buffer(self._h_control, CONTROL_MAX)
        self._ble.gatts_write(self._h_info, info)
        self._ble.irq(self._irq)
        self._resp = scan_response(name)
        self._last = None
        self.conn = None
        self.mtu = 23
        self.last_rx = 0
        self.commands = []
        self._batch = []
        self._seq = 0
        self.sent = 0
        self.dropped = 0

    def _irq(self, event, data):
        if event == _IRQ_CENTRAL_CONNECT:
            self.conn = data[0]
            self.last_rx = time.ticks_ms()
            self._batch = []
        elif event == _IRQ_CENTRAL_DISCONNECT:
            self.conn = None
            self._last = None  # force a fresh advert on the next publish
        elif event == _IRQ_GATTS_WRITE:
            if data[1] == self._h_control:
                self.last_rx = time.ticks_ms()
                self.commands.append(bytes(self._ble.gatts_read(self._h_control)))
        elif event == _IRQ_MTU_EXCHANGED:
            self.mtu = data[1]

    @property
    def connected(self):
        return self.conn is not None

    def set_info(self, info):
        self._ble.gatts_write(self._h_info, info)

    def check_link(self):
        """Drop a central that has stopped pinging so advertising resumes."""
        conn = self.conn
        if conn is not None and time.ticks_diff(time.ticks_ms(), self.last_rx) > LINK_TIMEOUT_MS:
            try:
                self._ble.gap_disconnect(conn)
            except OSError:
                pass

    def set_name(self, name):
        self._resp = scan_response(name)
        self._last = None
        try:
            self._ble.config(gap_name=name[:20])
        except Exception:
            pass

    def publish(self, mfg):
        if self.conn is not None or mfg == self._last:
            return
        self._last = mfg
        self._ble.gap_advertise(ADV_INTERVAL_US, adv_data=adv_payload(mfg), resp_data=self._resp)

    def stream(self, sample):
        conn = self.conn  # the disconnect IRQ may clear self.conn at any time
        if conn is None:
            return
        self._batch.append(sample)
        per_packet = min(STREAM_BATCH, (self.mtu - 6) // len(sample))
        if len(self._batch) < max(1, per_packet):
            return
        payload = pack_samples(self._seq, self._batch)
        self._batch = []
        self._seq += 1
        try:
            self._ble.gatts_notify(conn, self._h_stream, payload)
            self.sent += 1
        except (OSError, TypeError):
            self.dropped += 1

    def close(self):
        try:
            if self.conn is not None:
                self._ble.gap_disconnect(self.conn)
        except Exception:
            pass
        try:
            self._ble.gap_advertise(None)
            self._ble.active(False)
        except OSError:
            pass
