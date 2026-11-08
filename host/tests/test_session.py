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
             position=p.Position.LEFT_FOOT, gyro_range_dps=2000, samples_recorded=0,
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


def write_session(folder, true_rate=200.6, n_packets=200, drop_after=None, drop_samples=0,
                  dup_after=None, backlog=0):
    """Fake recording: 19-sample packets with a known clock relation.

    ``dup_after``: from that packet on, log every packet twice.
    ``backlog``: the first that many packets arrive in a burst just before
    the next one, as when a recorder joins a running recording."""
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
        if k < backlog:
            join_ns = offset_ns + skew * (5e6 + backlog * 19e6 / true_rate) * 1000
            if k == 0:
                log.write(RecordKind.STATUS, status(buffer_fill=backlog * 19).pack(), int(join_ns) - 1_000_000)
            rx = join_ns + k * 0.2e6  # drained faster than real time, before packet `backlog` is due
        elif backlog and k == backlog:
            log.write(RecordKind.STATUS, status(buffer_fill=0).pack(), int(rx) - 1)
        pkt = p.DataPacket.pack_imu6(seq, idx, int(ts_us), samples)
        log.write(RecordKind.DATA, pkt, int(rx))
        if dup_after is not None and k >= dup_after:
            log.write(RecordKind.DATA, pkt, int(rx) + 2_000_000)
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
    assert meta["tags"][0]["position"] == "left_foot"
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
    # The fake rx time is one sample period after the packet's last sample
    # plus >= 3 ms latency; the fit maps last-sample time to that minimum.
    tag_us = np.array([5e6, 200e6])
    true_min = offset_ns + skew * tag_us * 1000 + 1 / 200.6 * 1e9 + 3e6
    assert np.all(np.abs(fit.rx_clock(tag_us) - true_min) < 0.5e6)


def test_gps(tmp_path):
    w = SessionWriter(tmp_path / "s")
    w.gps_fix(elapsed_ns=1, utc_ms=2, lat=1.5, lon=2.5, speed_mps=7.0)
    w.close()
    g = load_session(tmp_path / "s").gps
    assert g["speed_mps"][0] == 7.0 and np.isnan(g["alt_m"][0])


def test_duplicate_packets_are_dropped(tmp_path):
    write_session(tmp_path / "s", dup_after=120)
    t = load_session(tmp_path / "s").tags[0]
    assert t.duplicate_packets == 80
    assert len(t.pkt_seq) == 200 and t.seq_gaps() == 0 and t.gaps() == []
    assert np.array_equal(t.sample_index, np.arange(200 * 19))


def test_clock_fit_ignores_backlog_after_join(tmp_path):
    # 40 % of the log is a buffer drained right after joining: as late
    # arrivals they'd pull a plain envelope fit far off.
    offset_ns, skew = write_session(tmp_path / "s", n_packets=1000, backlog=400)
    t = load_session(tmp_path / "s").tags[0]
    fit = fit_clocks(t)
    tag_us = np.array([5e6, 90e6])
    true_min = offset_ns + skew * tag_us * 1000 + 1 / 200.6 * 1e9 + 3e6
    assert np.all(np.abs(fit.rx_clock(tag_us) - true_min) < 1e6)
    assert fit.rx_residual_ms < 1.0


def test_clock_fit_too_short_to_judge(tmp_path):
    write_session(tmp_path / "s", n_packets=100)  # under 10 s: one fit window
    assert np.isnan(fit_clocks(load_session(tmp_path / "s").tags[0]).rx_residual_ms)


def test_resent_packets_after_reconnect(tmp_path):
    # The tag resends packets that weren't acknowledged when the link
    # dropped (PROTOCOL.md 1.9), from the last acknowledged seq. With a
    # different MTU on the new link they're cut differently, so they aren't
    # exact copies: seq goes back and samples overlap.
    w = SessionWriter(tmp_path / "s")
    log = w.add_tag(ADDR, INFO)
    log.write(RecordKind.STATUS, status().pack(), 0)
    samples = np.zeros((19, 6), np.int16)
    t = 1
    # After the drop the tag goes back to seq 1: seq 1 comes again with a
    # timestamp off by a few us (it's interpolated again), seq 2 cut
    # differently, then new data.
    for seq, idx, n, ts in [(0, 0, 19, 0), (1, 19, 19, 95000), (2, 38, 19, 190000),
                            (1, 19, 19, 95003), (2, 38, 10, 190000), (3, 48, 10, 240000),
                            (4, 58, 10, 290000)]:
        samples[:, 0] = np.arange(idx, idx + 19)
        log.write(RecordKind.DATA, p.DataPacket.pack_imu6(seq, idx, ts, samples[:n]), t)
        t += 1
    w.close()
    tag = load_session(tmp_path / "s").tags[0]
    assert tag.gaps() == [] and tag.seq_gaps() == 0
    assert np.array_equal(tag.sample_index, np.arange(68))
    assert np.array_equal(tag.raw[:, 0], np.arange(68))


def test_seq_gaps_counts_only_seqs_never_received(tmp_path):
    w = SessionWriter(tmp_path / "s")
    log = w.add_tag(ADDR, INFO)
    samples = np.zeros((19, 6), np.int16)
    # Across the wrap at 65536, a resend from 65534, and seq 2 never seen.
    for i, seq in enumerate([65533, 65534, 65535, 0, 65534, 65535, 0, 1, 3]):
        log.write(RecordKind.DATA, p.DataPacket.pack_imu6(seq, i * 19, i * 95000, samples), i)
    w.close()
    assert load_session(tmp_path / "s").tags[0].seq_gaps() == 1


def test_simulated_flag(tmp_path):
    w = SessionWriter(tmp_path / "real")
    w.add_tag(ADDR, INFO)
    w.add_tag("FA:KE:00:00:00:01")  # the host fake tag is flagged by its address
    w.save()
    meta = json.loads((tmp_path / "real" / "session.json").read_text())
    assert [t["simulated"] for t in meta["tags"]] == [False, True]
    s = load_session(tmp_path / "real")
    assert [t.meta.simulated for t in s.tags] == [False, True]
    assert s.simulated

    w = SessionWriter(tmp_path / "only-real")
    w.add_tag(ADDR, INFO)
    assert not load_session(tmp_path / "only-real").simulated


def test_simulated_before_the_field(tmp_path):
    """Old sessions: the Android simulated tag's address, or sim_truth.json."""
    for name, addr, truth in (("android", "02:00:00:00:00:01", False), ("simulate", ADDR, True),
                              ("real", ADDR, False)):
        d = tmp_path / name
        d.mkdir()
        (d / "session.json").write_text(json.dumps({
            "format_version": 1, "recorder": {"name": "x", "version": "0"},
            "start_utc": "2026-11-07T14:25:30.123Z", "start_elapsed_ns": 0,
            "tags": [{"address": addr}], "notes": ""}))
        if truth:
            (d / "sim_truth.json").write_text("{}")
        assert load_session(d).simulated == (name != "real"), name
