import numpy as np
import pytest

from bladetag.cycling_signals import _repetition


def test_pedal_period_is_one_foot_cycle_and_mount_independent():
    fs = 50.
    t = np.arange(400) / fs
    phase = 2 * np.pi * (73 / 60) * t
    gyro = np.column_stack([120 * np.sin(phase), 40 * np.cos(phase), 12 * np.sin(2 * phase)])
    rotation, _ = np.linalg.qr(np.random.default_rng(2).normal(size=(3, 3)))
    for g in [gyro, gyro @ rotation]:
        rpm, strength, rms = _repetition(g, fs)
        assert rpm == pytest.approx(73, abs=.4)
        assert strength > .8
        assert rms > 30


def test_stationary_sensor_has_no_repetition():
    assert _repetition(np.zeros((400, 3)), 50) is None


def test_windows_exclude_buffered_samples_gaps_and_walking(monkeypatch):
    from types import SimpleNamespace
    from bladetag import cycling_signals as signals
    fs = 50.
    # Buffered samples before start, a missing interval, then walking-speed GPS.
    times = np.r_[np.arange(-10, 12, 1/fs), np.arange(20, 42, 1/fs)]
    idx = np.rint((times + 10) * fs).astype(int)
    phase = times * 2 * np.pi * 70 / 60
    gyro = np.column_stack([120 * np.sin(phase), 40 * np.cos(phase), phase * 0])
    tag = SimpleNamespace(meta=SimpleNamespace(position='left_foot'), sample_index=idx, gyro_dps=gyro)
    clock = SimpleNamespace(rate_hz=fs, sample_elapsed_ns=lambda indices: (indices/fs - 10) * 1e9)
    monkeypatch.setattr(signals, 'fit_clocks', lambda _: clock)
    gps = np.zeros(43, dtype=[(n, 'f8') for n in ['elapsed_ns', 'speed_mps', 'h_acc_m', 'speed_acc_mps']])
    gps['elapsed_ns'] = np.arange(43) * 1e9
    gps['h_acc_m'] = 4
    gps['speed_acc_mps'] = .2
    gps['speed_mps'] = np.where(np.arange(43) < 20, 6., 1.)
    session = SimpleNamespace(tags=[tag], start_elapsed_ns=0, meta={'end_elapsed_ns': 42e9}, gps=gps)
    rows = signals.cadence_track(session)
    assert rows
    assert all(r['t_s'] >= 4 for r in rows)
    assert all(r['t_s'] <= 8 or r['t_s'] >= 24 for r in rows)
    assert any(r['pedaling'] for r in rows if r['t_s'] < 12)
    assert all(r['rpm'] is None for r in rows if r['t_s'] >= 20)
    session.gps = None
    assert all(r['rpm'] is None for r in signals.cadence_track(session))
