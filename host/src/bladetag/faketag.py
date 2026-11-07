"""In-process fake tag implementing the BLE protocol state machine.

Used by ``bladetag record fake`` and the tests, to check the recorder before
real firmware exists. Samples come from :mod:`bladetag.sim` and play in real
time (or faster, with ``speed``).
"""

from __future__ import annotations

import asyncio
import struct
import time
from collections import deque

import numpy as np

from . import protocol as p
from .link import Callback, ControlError, TagLink
from .sim import SkaterModel, quantize, simulate_motion


class FakeTag:
    _instances: dict[str, "FakeTag"] = {}

    def __init__(self, address: str = "FA:KE:00:00:00:01", speed: float = 1.0, loop_s: float = 120.0,
                 buffer_capacity: int = 8000, rate_error: float = 1.004, seed: int = 0):
        self.address = address
        self.speed = speed
        self.state = p.State.READY
        self.position = p.Position.UNKNOWN
        self.session_id = 0
        self.odr_hz, self.accel_range_g, self.gyro_range_dps = 200, 16, 2000
        self.buffer_capacity = buffer_capacity
        self.rate_error = rate_error
        self._boot = time.monotonic()
        self._seed = seed
        self._loop_s = loop_s
        self._motion = None
        self._buf: deque[bytes] = deque()
        self._buf_samples = 0
        self._link: FakeLink | None = None
        self._task: asyncio.Task | None = None
        self._status_task: asyncio.Task | None = None
        self.recorded = self.dropped = 0
        self._seq = 0
        self.disconnected_since: float | None = None
        self.link_loss_stop_s = 300.0
        self.identify_until = 0.0

    @classmethod
    def shared(cls, name: str) -> "FakeTag":
        """One fake tag per name, so reconnects find the same tag."""
        if name not in cls._instances:
            n = len(cls._instances) + 1
            cls._instances[name] = cls(address=f"FA:KE:00:00:00:{n:02X}")
        return cls._instances[name]

    # -- clocks
    def now_us(self) -> int:
        return int((time.monotonic() - self._boot) * 1e6 * self.speed)

    # -- characteristics
    def info(self) -> bytes:
        return p.Info(1, (0, 0, 1), list(p.ODR_VALUES), list(p.ACCEL_RANGES), list(p.GYRO_RANGES),
                      self.buffer_capacity).pack()

    def status(self) -> bytes:
        flags = p.StatusFlag.OVERFLOW if self.dropped else 0
        return p.Status(self.state, flags, 2980, self.session_id, self.odr_hz, self.accel_range_g,
                        self.position, self.gyro_range_dps, self.recorded, self.dropped,
                        self._buf_samples, self.now_us()).pack()

    def control(self, data: bytes) -> None:
        if not data:
            raise ControlError(p.ErrorCode.BAD_LENGTH)
        op, args = data[0], data[1:]
        if op == p.Opcode.START:
            if len(args) not in (0, 5):
                raise ControlError(p.ErrorCode.BAD_LENGTH)
            if self.state != p.State.READY:
                raise ControlError(p.ErrorCode.BAD_STATE)
            if self._link is None or not self._link._data_on or self._link.mtu < p.MIN_MTU:
                raise ControlError(p.ErrorCode.BAD_STATE)
            odr, acc, gyr = struct.unpack("<HBH", args) if args else (0, 0, 0)
            odr, acc, gyr = odr or 200, acc or 16, gyr or 2000
            if odr not in p.ODR_VALUES or acc not in p.ACCEL_RANGES or gyr not in p.GYRO_RANGES:
                raise ControlError(p.ErrorCode.BAD_PARAM)
            self.odr_hz, self.accel_range_g, self.gyro_range_dps = odr, acc, gyr
            self._start()
        elif op == p.Opcode.STOP:
            if len(args):
                raise ControlError(p.ErrorCode.BAD_LENGTH)
            if self.state == p.State.RECORDING:
                self._stop(flush=True)
        elif op == p.Opcode.IDENTIFY:
            if len(args) != 1:
                raise ControlError(p.ErrorCode.BAD_LENGTH)
            self.identify_until = time.monotonic() + args[0]
        elif op == p.Opcode.SET_POSITION:
            if len(args) != 1:
                raise ControlError(p.ErrorCode.BAD_LENGTH)
            if self.state != p.State.READY:
                raise ControlError(p.ErrorCode.BAD_STATE)
            if args[0] > 2:
                raise ControlError(p.ErrorCode.BAD_PARAM)
            self.position = p.Position(args[0])
        elif op == p.Opcode.SLEEP:
            if self.state != p.State.READY:
                raise ControlError(p.ErrorCode.BAD_STATE)
            if self._link:
                asyncio.get_running_loop().call_soon(self._link.drop)
        else:
            raise ControlError(p.ErrorCode.UNKNOWN_OPCODE)

    # -- recording
    def _start(self) -> None:
        self.session_id += 1
        self._seq = 0
        self.recorded = self.dropped = 0
        self._buf.clear()
        self._buf_samples = 0
        self.state = p.State.RECORDING
        if self._motion is None or self._motion[0] != self.odr_hz:
            rate = self.odr_hz * self.rate_error
            m = SkaterModel(duration_s=self._loop_s, pause=None, still_s=3.0, seed=self._seed)
            _, acc, gyr, _, _ = simulate_motion(m, rate)
            self._motion = (self.odr_hz, rate, quantize(acc, gyr, self.accel_range_g, self.gyro_range_dps))
        self._notify_status()
        self._task = asyncio.get_running_loop().create_task(self._sample_loop())

    def _stop(self, flush: bool) -> None:
        if self._task:
            self._task.cancel()
            self._task = None
        if flush:
            self._flush()
        else:
            self._buf.clear()
            self._buf_samples = 0
        self.state = p.State.READY
        self._notify_status()

    async def _sample_loop(self) -> None:
        _, rate, raw = self._motion
        spp = p.samples_per_packet(self._link.mtu if self._link else p.MTU)
        t0_us = self.now_us()
        idx = 0
        while True:
            # Wait until the next packet's last sample "has been taken".
            due_us = t0_us + (idx + spp) * 1e6 / rate
            await asyncio.sleep(max(0.0, (due_us - self.now_us()) / 1e6 / self.speed))
            chunk = raw[np.arange(idx, idx + spp) % len(raw)]
            ts = int(t0_us + idx * 1e6 / rate)
            self.recorded += spp
            if self._buf_samples + spp > self.buffer_capacity:
                self.dropped += spp
            else:
                self._buf.append(p.DataPacket.pack_imu6(self._seq, idx, ts, chunk))
                self._buf_samples += spp
                self._seq += 1
            idx += spp
            self._flush()
            if (self.disconnected_since is not None
                    and time.monotonic() - self.disconnected_since > self.link_loss_stop_s / self.speed):
                self._stop(flush=False)
                return

    def _flush(self) -> None:
        link = self._link
        if link is None or not link._data_on:
            return
        while self._buf:
            pkt = self._buf.popleft()
            self._buf_samples -= pkt[1]
            link._on_data(pkt)

    def _notify_status(self) -> None:
        if self._link and self._link._status_on:
            self._link._on_status(self.status())

    async def _status_loop(self) -> None:
        while True:
            await asyncio.sleep((1.0 if self.state == p.State.RECORDING else 10.0) / self.speed)
            self._notify_status()

    # -- connection
    def attach(self, link: "FakeLink") -> None:
        if self._link is not None:
            raise ConnectionError("fake tag: already connected")
        self._link = link
        self.disconnected_since = None
        self._status_task = asyncio.get_running_loop().create_task(self._status_loop())

    def detach(self) -> None:
        self._link = None
        self.disconnected_since = time.monotonic()
        if self._status_task:
            self._status_task.cancel()
            self._status_task = None


class FakeLink(TagLink):
    def __init__(self, tag: FakeTag, mtu: int = p.MTU):
        super().__init__()
        self.tag = tag
        self.address = tag.address
        self._mtu = mtu
        self._connected = False
        self._status_on = self._data_on = False
        self._on_status: Callback = lambda b: None
        self._on_data: Callback = lambda b: None
        self.refuse_connect = False

    async def connect(self) -> None:
        await asyncio.sleep(0.01)
        if self.refuse_connect:
            raise TimeoutError("fake: not found")
        self.tag.attach(self)
        self._connected = True
        self._status_on = self._data_on = False

    async def disconnect(self) -> None:
        if self._connected:
            self._connected = False
            self.tag.detach()

    def drop(self) -> None:
        """Simulate link loss."""
        if self._connected:
            self._connected = False
            self._status_on = self._data_on = False
            self.tag.detach()
            if self.on_disconnect:
                self.on_disconnect()

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def mtu(self) -> int:
        return self._mtu

    def _check(self):
        if not self._connected:
            raise ConnectionError("fake: not connected")

    async def read_info(self) -> bytes:
        self._check()
        return self.tag.info()

    async def read_status(self) -> bytes:
        self._check()
        return self.tag.status()

    async def write_control(self, data: bytes) -> None:
        self._check()
        await asyncio.sleep(0.005)
        self.tag.control(bytes(data))

    async def subscribe(self, on_status: Callback, on_data: Callback) -> None:
        self._check()
        self._on_status, self._on_data = on_status, on_data
        self._status_on = self._data_on = True
        self.tag._flush()
