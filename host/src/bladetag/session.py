"""Session file format (PROTOCOL.md section 2): writer and reader.

The writer stores notifications verbatim. The reader decodes them into numpy
arrays, so a decoding bug never loses data.
"""

from __future__ import annotations

import csv
import io
import json
import struct
import time
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import IntEnum
from pathlib import Path
from typing import BinaryIO, Iterator

import numpy as np

from . import __version__
from . import protocol as p

FORMAT_VERSION = 1
RECORD_HEADER = struct.Struct("<QBH")
GPS_COLUMNS = [
    "elapsed_ns", "utc_ms", "lat", "lon", "alt_m",
    "speed_mps", "bearing_deg", "h_acc_m", "speed_acc_mps",
]


class RecordKind(IntEnum):
    DATA = 1
    STATUS = 2


def tag_filename(address: str) -> str:
    return "tag-" + address.replace(":", "").replace("-", "").upper() + ".bin"


def utc_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_utc(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


# --- Writing -----------------------------------------------------------------


class TagLog:
    """Append-only raw notification log for one tag."""

    def __init__(self, f: BinaryIO):
        self._f = f

    def write(self, kind: RecordKind, payload: bytes, elapsed_ns: int | None = None) -> None:
        if elapsed_ns is None:
            elapsed_ns = time.monotonic_ns()
        self._f.write(RECORD_HEADER.pack(elapsed_ns, kind, len(payload)) + bytes(payload))

    def flush(self) -> None:
        self._f.flush()

    def close(self) -> None:
        self._f.close()


@dataclass
class TagMeta:
    address: str
    position: str = "unknown"
    file: str = ""
    info_hex: str = ""
    session_id: int | None = None
    odr_hz: int | None = None
    accel_range_g: int | None = None
    gyro_range_dps: int | None = None

    def __post_init__(self):
        if not self.file:
            self.file = tag_filename(self.address)

    def update_from_status(self, s: p.Status) -> None:
        self.session_id = s.session_id
        self.odr_hz = s.odr_hz
        self.accel_range_g = s.accel_range_g
        self.gyro_range_dps = s.gyro_range_dps
        self.position = s.position.label


class SessionWriter:
    """Writes a session folder. ``session.json`` is rewritten on every
    :meth:`save` so a crash still leaves usable metadata."""

    def __init__(self, folder: Path, recorder_name: str = "bladetag-host",
                 start_utc: datetime | None = None, start_elapsed_ns: int | None = None):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=False)
        if start_elapsed_ns is None:
            start_elapsed_ns = time.monotonic_ns()
        if start_utc is None:
            start_utc = datetime.now(timezone.utc)
        self.meta: dict = {
            "format_version": FORMAT_VERSION,
            "recorder": {"name": recorder_name, "version": __version__},
            "start_utc": utc_iso(start_utc),
            "start_elapsed_ns": start_elapsed_ns,
            "end_elapsed_ns": None,
            "tags": [],
            "notes": "",
        }
        self.tags: dict[str, TagMeta] = {}
        self._logs: dict[str, TagLog] = {}
        self._gps: tuple[io.TextIOBase, csv.writer] | None = None
        self.save()

    @staticmethod
    def new_folder(parent: Path, when: datetime | None = None) -> Path:
        when = when or datetime.now()
        return Path(parent) / when.strftime("session-%Y%m%d-%H%M%S")

    def add_tag(self, address: str, info_raw: bytes = b"") -> TagLog:
        if address in self._logs:
            return self._logs[address]
        tm = TagMeta(address=address, info_hex=info_raw.hex())
        self.tags[address] = tm
        log = TagLog(open(self.folder / tm.file, "ab"))
        self._logs[address] = log
        self.save()
        return log

    def gps_fix(self, **fields) -> None:
        if self._gps is None:
            f = open(self.folder / "gps.csv", "w", newline="")
            w = csv.writer(f)
            w.writerow(GPS_COLUMNS)
            self._gps = (f, w)
        self._gps[1].writerow(["" if fields.get(c) is None else fields[c] for c in GPS_COLUMNS])

    def save(self) -> None:
        self.meta["tags"] = [vars(t) for t in self.tags.values()]
        tmp = self.folder / "session.json.tmp"
        tmp.write_text(json.dumps(self.meta, indent=2) + "\n")
        tmp.replace(self.folder / "session.json")

    def close(self, end_elapsed_ns: int | None = None) -> None:
        self.meta["end_elapsed_ns"] = end_elapsed_ns if end_elapsed_ns is not None else time.monotonic_ns()
        for log in self._logs.values():
            log.close()
        if self._gps:
            self._gps[0].close()
        self.save()


# --- Reading -----------------------------------------------------------------


def iter_records(data: bytes) -> Iterator[tuple[int, int, bytes]]:
    """Yield (elapsed_ns, kind, payload). A truncated last record is dropped."""
    off, n = 0, len(data)
    while off + RECORD_HEADER.size <= n:
        t, kind, ln = RECORD_HEADER.unpack_from(data, off)
        off += RECORD_HEADER.size
        if off + ln > n:
            break
        yield t, kind, data[off:off + ln]
        off += ln


@dataclass
class TagData:
    """Decoded log of one tag. Arrays are aligned per sample unless noted."""

    meta: TagMeta
    info: p.Info | None
    # per sample
    sample_index: np.ndarray            # int64
    raw: np.ndarray                     # (N, 6) int16
    # per packet
    pkt_rx_ns: np.ndarray               # int64
    pkt_seq: np.ndarray                 # int64
    pkt_sample_index: np.ndarray        # int64
    pkt_n: np.ndarray                   # int64
    pkt_timestamp_us: np.ndarray        # int64
    statuses: list[tuple[int, p.Status]] = field(default_factory=list)
    unknown_packets: int = 0
    bad_records: int = 0
    duplicate_packets: int = 0

    @property
    def odr_hz(self) -> int:
        return self.meta.odr_hz or p.DEFAULT_ODR_HZ

    @property
    def accel_g(self) -> np.ndarray:
        return self.raw[:, :3] * p.accel_scale_g(self.meta.accel_range_g or p.DEFAULT_ACCEL_RANGE_G)

    @property
    def gyro_dps(self) -> np.ndarray:
        return self.raw[:, 3:] * p.gyro_scale_dps(self.meta.gyro_range_dps or p.DEFAULT_GYRO_RANGE_DPS)

    def gaps(self) -> list[tuple[int, int]]:
        """(first missing sample_index, count) for every jump in sample_index."""
        if len(self.pkt_sample_index) < 2:
            return []
        expected = self.pkt_sample_index[:-1] + self.pkt_n[:-1]
        jumps = np.nonzero(self.pkt_sample_index[1:] != expected)[0]
        return [(int(expected[j]), int(self.pkt_sample_index[j + 1] - expected[j])) for j in jumps]

    def seq_gaps(self) -> int:
        if len(self.pkt_seq) < 2:
            return 0
        d = (np.diff(self.pkt_seq) - 1) % 65536
        return int(d.sum())


def decode_tag_log(data: bytes, meta: TagMeta) -> TagData:
    info = None
    if meta.info_hex:
        try:
            info = p.Info.parse(bytes.fromhex(meta.info_hex.replace(" ", "")))
        except (ValueError, p.ProtocolError):
            pass
    rx, seq, idx, n, ts, chunks = [], [], [], [], [], []
    statuses: list[tuple[int, p.Status]] = []
    unknown = bad = dup = 0
    seen: set[tuple[int, int, int]] = set()
    for t, kind, payload in iter_records(data):
        try:
            if kind == RecordKind.DATA:
                pkt = p.DataPacket.parse(payload)
                if pkt.samples is None:
                    unknown += 1
                    continue
                # BlueZ can deliver a notification twice (old notify sessions
                # after a reconnect). Keep the first copy. timestamp_us is in
                # the key so a new session in the same log isn't mistaken
                # for a copy.
                key = (pkt.seq, pkt.sample_index, pkt.timestamp_us)
                if key in seen:
                    dup += 1
                    continue
                seen.add(key)
                rx.append(t); seq.append(pkt.seq); idx.append(pkt.sample_index)
                n.append(pkt.n); ts.append(pkt.timestamp_us); chunks.append(pkt.samples)
            elif kind == RecordKind.STATUS:
                statuses.append((t, p.Status.parse(payload)))
        except (p.ProtocolError, ValueError):
            bad += 1

    n_arr = np.array(n, dtype=np.int64)
    idx_arr = np.array(idx, dtype=np.int64)
    raw = np.concatenate(chunks) if chunks else np.zeros((0, 6), np.int16)
    sample_index = (np.repeat(idx_arr, n_arr) + _ramp(n_arr)) if len(n_arr) else np.zeros(0, np.int64)

    # Duplicates can appear if a writer logs a packet twice; keep the first.
    if len(sample_index) and np.any(np.diff(sample_index) <= 0):
        order = np.argsort(sample_index, kind="stable")
        sample_index, raw = sample_index[order], raw[order]
        keep = np.concatenate([[True], np.diff(sample_index) > 0])
        sample_index, raw = sample_index[keep], raw[keep]

    if statuses and meta.odr_hz is None:
        meta.update_from_status(statuses[-1][1])
    return TagData(
        meta=meta, info=info, sample_index=sample_index, raw=raw,
        pkt_rx_ns=np.array(rx, dtype=np.int64), pkt_seq=np.array(seq, dtype=np.int64),
        pkt_sample_index=idx_arr, pkt_n=n_arr, pkt_timestamp_us=np.array(ts, dtype=np.int64),
        statuses=statuses, unknown_packets=unknown, bad_records=bad, duplicate_packets=dup,
    )


def _ramp(n: np.ndarray) -> np.ndarray:
    """[0..n0-1, 0..n1-1, ...]"""
    total = int(n.sum())
    starts = np.repeat(np.cumsum(n) - n, n)
    return np.arange(total, dtype=np.int64) - starts


@dataclass
class Session:
    path: Path
    meta: dict
    tags: list[TagData]
    gps: "np.ndarray | None"  # structured array with GPS_COLUMNS, or None

    @property
    def start_utc(self) -> datetime:
        return parse_utc(self.meta["start_utc"])

    @property
    def start_elapsed_ns(self) -> int:
        return int(self.meta["start_elapsed_ns"])

    def utc_at(self, elapsed_ns: float) -> datetime:
        return self.start_utc + timedelta(microseconds=(elapsed_ns - self.start_elapsed_ns) / 1e3)

    def tag(self, position: str | None = None) -> TagData:
        if position is None:
            return self.tags[0]
        for t in self.tags:
            if t.meta.position == position:
                return t
        raise KeyError(position)


class _Folder:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._zip = None
        self._prefix = ""
        if self.path.is_file() and zipfile.is_zipfile(self.path):
            self._zip = zipfile.ZipFile(self.path)
            jsons = [n for n in self._zip.namelist() if n.endswith("session.json")]
            if not jsons:
                raise FileNotFoundError(f"no session.json in {path}")
            self._prefix = min(jsons, key=len)[: -len("session.json")]

    def read(self, name: str) -> bytes | None:
        if self._zip:
            try:
                return self._zip.read(self._prefix + name)
            except KeyError:
                return None
        f = self.path / name
        return f.read_bytes() if f.exists() else None


def load_session(path: str | Path) -> Session:
    """Load a session folder or a .zip of one."""
    folder = _Folder(Path(path))
    meta_raw = folder.read("session.json")
    if meta_raw is None:
        raise FileNotFoundError(f"no session.json in {path}")
    meta = json.loads(meta_raw)
    if meta.get("format_version") != FORMAT_VERSION:
        raise ValueError(f"unsupported format_version {meta.get('format_version')}")
    known = set(TagMeta.__dataclass_fields__)
    tags = []
    for t in meta.get("tags", []):
        tm = TagMeta(**{k: v for k, v in t.items() if k in known})
        data = folder.read(tm.file) or b""
        tags.append(decode_tag_log(data, tm))
    gps_raw = folder.read("gps.csv")
    return Session(Path(path), meta, tags, _parse_gps(gps_raw) if gps_raw else None)


def _parse_gps(raw: bytes) -> np.ndarray | None:
    rows = list(csv.DictReader(io.StringIO(raw.decode())))
    if not rows:
        return None
    dt = np.dtype([(c, "f8") for c in GPS_COLUMNS])
    out = np.full(len(rows), np.nan, dtype=dt)
    for i, r in enumerate(rows):
        for c in GPS_COLUMNS:
            v = r.get(c, "")
            if v not in ("", None):
                out[c][i] = float(v)
    return out
