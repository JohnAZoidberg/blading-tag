import json
import shutil
from datetime import datetime, timezone

import numpy as np
import pytest

from bladetag import protocol as p
from bladetag.session import (
    RECORD_HEADER, RecordKind, SessionWriter, iter_records, load_session, tag_filename,
)
from bladetag.timesync import fit_clocks

ADDR = "C0:BB:4A:A7:0D:57"
INFO = bytes.fromhex("010001003f000f1f401f0000")


def status(state=p.State.RECORDING, **kw):
    d = dict(state=state, flags=0, battery_mv=3000, session_id=7, odr_hz=200, accel_range_g=16,
             position=p.Position.LEFT, gyro_range_dps=2000, samples_recorded=0,
             samples_dropped=0, buffer_fill=0, tag_time_us=0)
    d.update(kw)
    return p.Status(**d)


def test_record_header_example():
    assert RECORD_HEADER.pack(123456789012, 1, 40).hex(" ") == "14 1a 99 be 1c 00 00 00 01 28 00"


def test_tag_filename():
    assert tag_filename(ADDR) == "tag-C0BB4AA70D57.bin"


def test_truncated_record_is_dropped():
    good = RECORD_HEADER.pack(1, 1, 3) + b"abc"
    assert list(iter_records(good + RECORD_HEADER.pack(2, 1, 10) + b"xy")) == [(1, 1, b"abc")]


def write_session(folder, true_rate=200.6, n_packets=200, drop_after=None, drop_samples=0):
    """Fake recording: 19-sample packets with a known clock relation."""
    w = SessionWriter(folder, start_utc=datetime(2026, 11, 7, 14, 25, 30, tzinfo=timezone.utc),
                      start_elapsed_ns=10_000_000_000)
    log = w.add_tag(ADDR, INFO)
    log.write(RecordKind.STATUS, status(state=p.State.READY).pack(), 10_000_000_000)
    rng = np.random.default_rng(1)
    offset_ns, skew = 10_100_000_000, 1.00002  # rx = offset + skew * tag_ns
    idx, seq = 0, 0
    for k in range(n_packets):
        if drop_after is not None and k == drop_after:
            idx += drop_samples
        ts_us = 5_000_000 + idx * 1e6 / true_rate
        samples = np.tile(np.array([k, -k, 2048, 1, 2, 3], np.int16), (19, 1))
        samples[:, 0] = np.arange(idx, idx + 19) % 30000
        rx = offset_ns + skew * ts_us * 1000 + 19 / true_rate * 1e9 + rng.exponential(8e6) + 3e6
        log.write(RecordKind.DATA, p.DataPacket.pack_imu6(seq, idx, int(ts_us), samples), int(rx))
        idx += 19
        seq += 1
    w.tags[ADDR].update_from_status(status())
    w.close(end_elapsed_ns=int(rx) + 1)
    return offset_ns, skew


def test_roundtrip(tmp_path):
    folder = tmp_path / "s"
    write_session(folder)
    meta = json.loads((folder / "session.json").read_text())
    assert meta["tags"][0]["file"] == "tag-C0BB4AA70D57.bin"
    assert meta["tags"][0]["position"] == "left"
    s = load_session(folder)
    t = s.tag("left")
    assert t.info.buffer_capacity == 8000
    assert len(t.raw) == 200 * 19
    assert np.array_equal(t.sample_index, np.arange(200 * 19))
    assert np.array_equal(t.raw[:, 0], np.arange(200 * 19) % 30000)
    assert t.accel_g[0, 2] == pytest.approx(1.0)
    assert t.gaps() == [] and t.seq_gaps() == 0
    assert len(t.statuses) == 1
    assert s.utc_at(s.start_elapsed_ns + 1_500_000_000).isoformat() == "2026-11-07T14:25:31.500000+00:00"


def test_zip(tmp_path):
    folder = tmp_path / "session-x"
    write_session(folder)
    z = shutil.make_archive(str(tmp_path / "export"), "zip", tmp_path, "session-x")
    s = load_session(z)
    assert len(s.tags[0].raw) == 200 * 19


def test_gaps(tmp_path):
    write_session(tmp_path / "s", drop_after=50, drop_samples=123)
    t = load_session(tmp_path / "s").tags[0]
    assert t.gaps() == [(50 * 19, 123)]


def test_clock_fit(tmp_path):
    offset_ns, skew = write_session(tmp_path / "s", n_packets=3000)
    t = load_session(tmp_path / "s").tags[0]
    fit = fit_clocks(t)
    assert fit.rate_hz == pytest.approx(200.6, rel=1e-5)
    # The envelope carries the fixed 1-packet + 3 ms latency; the fitted
    # mapping should be within a couple of ms of the true minimum delay.
    tag_us = np.array([5e6, 200e6])
    true_min = offset_ns + skew * tag_us * 1000 + 19 / 200.6 * 1e9 + 3e6
    assert np.all(np.abs(fit.rx_clock(tag_us) - true_min) < 2e6)


def test_gps(tmp_path):
    w = SessionWriter(tmp_path / "s")
    w.gps_fix(elapsed_ns=1, utc_ms=2, lat=1.5, lon=2.5, speed_mps=7.0)
    w.close()
    g = load_session(tmp_path / "s").gps
    assert g["speed_mps"][0] == 7.0 and np.isnan(g["alt_m"][0])
