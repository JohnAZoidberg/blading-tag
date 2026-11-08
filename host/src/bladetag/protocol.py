"""BLE protocol v1 codec (PROTOCOL.md section 1).

Pure functions and dataclasses, no BLE dependency, so the recorder, the
simulator and the analysis all share one implementation.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from enum import IntEnum

import numpy as np

PROTOCOL_VERSION = 1
COMPANY_ID = 0xFFFF


def _uuid(short: int) -> str:
    return f"b1ad{short:04x}-0e83-479a-bb08-19dab75f76db"


SERVICE_UUID = _uuid(0x0001)
INFO_UUID = _uuid(0x0002)
CONTROL_UUID = _uuid(0x0003)
STATUS_UUID = _uuid(0x0004)
DATA_UUID = _uuid(0x0005)
SMP_UUID = "8d53dc1d-1db7-4cd3-868b-8a527460aa84"

MTU = 247
MIN_MTU = 64

ODR_VALUES = (25, 50, 100, 200, 400, 800)
ACCEL_RANGES = (2, 4, 8, 16)
GYRO_RANGES = (125, 250, 500, 1000, 2000)

DEFAULT_ODR_HZ = 200
DEFAULT_ACCEL_RANGE_G = 16
DEFAULT_GYRO_RANGE_DPS = 2000


class State(IntEnum):
    READY = 0
    RECORDING = 1


class Position(IntEnum):
    """Where on the body the tag is worn."""
    UNKNOWN = 0
    LEFT_FOOT = 1
    RIGHT_FOOT = 2
    LEFT_WRIST = 3
    RIGHT_WRIST = 4
    LEFT_POCKET = 5
    RIGHT_POCKET = 6
    BACKPACK = 7

    @property
    def label(self) -> str:
        return self.name.lower()

    @classmethod
    def from_label(cls, label: str) -> "Position":
        return cls[normalize_position(label).upper()]


# Sessions and scripts from before body locations said just "left"/"right" (feet).
_LEGACY_POSITIONS = {"left": "left_foot", "right": "right_foot"}


def normalize_position(label: str) -> str:
    """"left" -> "left_foot" etc.; current labels are returned as they are."""
    return _LEGACY_POSITIONS.get(label, label)


class Opcode(IntEnum):
    START = 0x01
    STOP = 0x02
    IDENTIFY = 0x03
    SET_POSITION = 0x04
    SLEEP = 0x05


class ErrorCode(IntEnum):
    UNKNOWN_OPCODE = 0x80
    BAD_LENGTH = 0x81
    BAD_PARAM = 0x82
    BAD_STATE = 0x83
    SENSOR_FAULT = 0x84


class StatusFlag(IntEnum):
    IMU_FAULT = 1 << 0
    OVERFLOW = 1 << 1
    LOW_BATTERY = 1 << 2


class PacketType(IntEnum):
    IMU6 = 0x01


class ProtocolError(ValueError):
    pass


def _mask_values(mask: int, values: tuple[int, ...]) -> list[int]:
    return [v for i, v in enumerate(values) if mask & (1 << i)]


def _values_mask(selected: list[int], values: tuple[int, ...]) -> int:
    return sum(1 << values.index(v) for v in selected)


# --- Advertising -------------------------------------------------------------


@dataclass(frozen=True)
class Advert:
    protocol_version: int
    state: State
    position: Position
    battery_mv: int

    _FMT = struct.Struct("<BBBH")

    @classmethod
    def parse(cls, data: bytes) -> "Advert":
        """Parse manufacturer data *without* the company ID.

        bleak's ``AdvertisementData.manufacturer_data`` is keyed by company ID
        and holds the rest, which is what this takes.
        """
        if len(data) < cls._FMT.size:
            raise ProtocolError(f"advert too short: {len(data)} B")
        ver, state, pos, mv = cls._FMT.unpack_from(data)
        return cls(ver, State(state), Position(pos), mv)

    def pack(self) -> bytes:
        return self._FMT.pack(self.protocol_version, self.state, self.position, self.battery_mv)


def local_name(address: str) -> str:
    """``BladeTag-XXXX`` from the last two address bytes."""
    parts = address.replace("-", ":").split(":")
    return "BladeTag-" + (parts[-2] + parts[-1]).upper()


# --- Info --------------------------------------------------------------------


@dataclass(frozen=True)
class Info:
    protocol_version: int
    fw_version: tuple[int, int, int]
    odr_hz: list[int]
    accel_ranges_g: list[int]
    gyro_ranges_dps: list[int]
    buffer_capacity: int

    _FMT = struct.Struct("<BBBBHBBI")

    @classmethod
    def parse(cls, data: bytes) -> "Info":
        if len(data) < cls._FMT.size:
            raise ProtocolError(f"Info too short: {len(data)} B")
        ver, ma, mi, pa, odr, acc, gyr, cap = cls._FMT.unpack_from(data)
        return cls(
            ver,
            (ma, mi, pa),
            _mask_values(odr, ODR_VALUES),
            _mask_values(acc, ACCEL_RANGES),
            _mask_values(gyr, GYRO_RANGES),
            cap,
        )

    def pack(self) -> bytes:
        return self._FMT.pack(
            self.protocol_version,
            *self.fw_version,
            _values_mask(self.odr_hz, ODR_VALUES),
            _values_mask(self.accel_ranges_g, ACCEL_RANGES),
            _values_mask(self.gyro_ranges_dps, GYRO_RANGES),
            self.buffer_capacity,
        )

    @property
    def fw_string(self) -> str:
        return "%d.%d.%d" % self.fw_version


# --- Status ------------------------------------------------------------------


@dataclass(frozen=True)
class Status:
    state: State
    flags: int
    battery_mv: int
    session_id: int
    odr_hz: int
    accel_range_g: int
    position: Position
    gyro_range_dps: int
    samples_recorded: int
    samples_dropped: int
    buffer_fill: int
    tag_time_us: int

    _FMT = struct.Struct("<BBHHHBBHIIIQ")
    SIZE = _FMT.size  # 32

    @classmethod
    def parse(cls, data: bytes) -> "Status":
        if len(data) < cls.SIZE:
            raise ProtocolError(f"Status too short: {len(data)} B")
        f = cls._FMT.unpack_from(data)
        return cls(State(f[0]), f[1], f[2], f[3], f[4], f[5], Position(f[6]), *f[7:])

    def pack(self) -> bytes:
        return self._FMT.pack(
            self.state, self.flags, self.battery_mv, self.session_id, self.odr_hz,
            self.accel_range_g, self.position, self.gyro_range_dps, self.samples_recorded,
            self.samples_dropped, self.buffer_fill, self.tag_time_us,
        )

    def has(self, flag: StatusFlag) -> bool:
        return bool(self.flags & flag)

    def flag_names(self) -> list[str]:
        return [f.name for f in StatusFlag if self.flags & f]


# --- Data --------------------------------------------------------------------

DATA_HEADER = struct.Struct("<BBHIQ")
DATA_HEADER_SIZE = DATA_HEADER.size  # 16
IMU6_SAMPLE_SIZE = 12
IMU6_DTYPE = np.dtype("<i2")


def samples_per_packet(mtu: int = MTU) -> int:
    return (mtu - 3 - DATA_HEADER_SIZE) // IMU6_SAMPLE_SIZE


@dataclass(frozen=True)
class DataPacket:
    type: int
    n: int
    seq: int
    sample_index: int
    timestamp_us: int
    samples: np.ndarray | None  # (n, 6) int16 for IMU6, None for unknown types

    @classmethod
    def parse(cls, data: bytes) -> "DataPacket":
        if len(data) < DATA_HEADER_SIZE:
            raise ProtocolError(f"Data packet too short: {len(data)} B")
        typ, n, seq, idx, ts = DATA_HEADER.unpack_from(data)
        samples = None
        if typ == PacketType.IMU6:
            end = DATA_HEADER_SIZE + n * IMU6_SAMPLE_SIZE
            if len(data) < end:
                raise ProtocolError(f"IMU6 packet with n={n} needs {end} B, got {len(data)}")
            samples = np.frombuffer(data, IMU6_DTYPE, n * 6, DATA_HEADER_SIZE).reshape(n, 6)
        return cls(typ, n, seq, idx, ts, samples)

    @staticmethod
    def pack_imu6(seq: int, sample_index: int, timestamp_us: int, samples: np.ndarray) -> bytes:
        samples = np.asarray(samples, dtype=IMU6_DTYPE).reshape(-1, 6)
        hdr = DATA_HEADER.pack(PacketType.IMU6, len(samples), seq & 0xFFFF, sample_index, timestamp_us)
        return hdr + samples.tobytes()


# --- Control -----------------------------------------------------------------


def cmd_start(odr_hz: int = 0, accel_range_g: int = 0, gyro_range_dps: int = 0) -> bytes:
    """START. Zeros mean 'tag default'; all zeros sends the 1-byte form."""
    if odr_hz == accel_range_g == gyro_range_dps == 0:
        return bytes([Opcode.START])
    return struct.pack("<BHBH", Opcode.START, odr_hz, accel_range_g, gyro_range_dps)


def cmd_stop() -> bytes:
    return bytes([Opcode.STOP])


def cmd_identify(seconds: int) -> bytes:
    return bytes([Opcode.IDENTIFY, seconds])


def cmd_set_position(position: Position) -> bytes:
    return bytes([Opcode.SET_POSITION, position])


def cmd_sleep() -> bytes:
    return bytes([Opcode.SLEEP])


# --- Units -------------------------------------------------------------------

def accel_scale_g(accel_range_g: int) -> float:
    return accel_range_g / 32768.0


def gyro_scale_dps(gyro_range_dps: int) -> float:
    return gyro_range_dps / 32768.0
