"""Conservative pedal repetition estimates from a foot-mounted IMU.

These are crank revolutions (one cycle of the measured foot), not skating
pushes. Windows without a reliable moving GPS fix or a repetitive rotation
remain unclassified: they are not automatically labelled coasting.
"""
from __future__ import annotations

import numpy as np

from .session import Session
from .timesync import fit_clocks


def _repetition(gyro: np.ndarray, fs: float) -> tuple[float, float, float] | None:
    """First strong autocorrelation peak, avoiding multiples of the cycle."""
    x = gyro - gyro.mean(axis=0)
    rms = float(np.sqrt(np.mean(np.sum(x * x, axis=1))))
    n = len(x)
    nfft = 1 << (2 * n - 1).bit_length()
    f = np.fft.rfft(x, nfft, axis=0)
    ac = np.fft.irfft(np.abs(f) ** 2, nfft, axis=0)[:n].sum(axis=1)
    if ac[0] <= 0:
        return None
    ac /= ac[0]
    peaks = np.flatnonzero((ac[1:-1] > ac[:-2]) & (ac[1:-1] >= ac[2:])) + 1
    peaks = peaks[(peaks >= fs * 60 / 140) & (peaks <= fs * 60 / 35)]
    peaks = peaks[ac[peaks] > 0]
    if not len(peaks):
        return None
    eligible = peaks[ac[peaks] >= .8 * ac[peaks].max()]
    k = int(eligible[0])
    # Refine the lag to avoid quantizing the displayed cadence.
    den = ac[k - 1] - 2 * ac[k] + ac[k + 1]
    delta = .5 * (ac[k - 1] - ac[k + 1]) / den if den else 0.
    return float(60 * fs / (k + np.clip(delta, -.5, .5))), float(ac[k]), rms


def cadence_track(sess: Session, win_s: float = 8., step_s: float = 2.) -> list[dict]:
    """Window-centre cadence candidates, with a conservative ``pedaling`` flag.

    ``rpm`` is None unless repetition strength >= .6, rotational RMS >=
    30 dps, and GPS speed >= 8 km/h with accuracy <=20 m and speed accuracy
    <=2 m/s. Missing GPS fixes and gaps >5 s are unclassified. All windows
    lie inside the recording and a single continuous IMU segment. GPS speed
    excludes most walking, but fast running cannot be distinguished solely
    by this gate; this estimator is intended for sessions marked cycling.
    """
    if win_s <= 0 or step_s <= 0:
        raise ValueError('window and step must be positive')
    foot = next((tag for tag in sess.tags if tag.meta.position in ('left_foot', 'right_foot')), None)
    if foot is None or not len(foot.sample_index):
        return []
    clock = fit_clocks(foot)
    t = (clock.sample_elapsed_ns(foot.sample_index) - sess.start_elapsed_ns) / 1e9
    end = (sess.meta.get('end_elapsed_ns', sess.start_elapsed_ns + max(0., t[-1]) * 1e9) - sess.start_elapsed_ns) / 1e9
    valid = (t >= 0) & (t <= end)
    idx = foot.sample_index[valid]
    t = t[valid]
    gyro = foot.gyro_dps[valid]
    if not len(t):
        return []
    # Downsample to ~50 Hz; angular motion is far below its Nyquist rate.
    stride = max(1, int(clock.rate_hz / 50))
    fs = clock.rate_hz / stride
    n = max(3, round(win_s * clock.rate_hz))
    step = max(1, round(step_s * clock.rate_hz))
    bounds = np.r_[0, np.flatnonzero(np.diff(idx) != 1) + 1, len(idx)]
    gps = sess.gps
    if gps is not None and len(gps):
        gt = (gps['elapsed_ns'] - sess.start_elapsed_ns) / 1e9
        order = np.argsort(gt)
        gt, gps = gt[order], gps[order]
    else:
        gt = np.array([])
    rows = []
    for a, b in zip(bounds[:-1], bounds[1:]):
        for start in range(int(a), int(b) - n + 1, step):
            center = float(t[start + n // 2])
            estimate = _repetition(gyro[start:start + n:stride], fs)
            if estimate is None:
                continue
            candidate, strength, rms = estimate
            speed = None
            if len(gt):
                j = int(np.argmin(np.abs(gt - center)))
                fix = gps[j]
                if (abs(gt[j] - center) <= 2.5 and np.isfinite(fix['speed_mps'])
                        and 0 <= fix['h_acc_m'] <= 20
                        and 0 <= fix['speed_acc_mps'] <= 2):
                    speed = float(fix['speed_mps'] * 3.6)
            pedaling = bool(strength >= .6 and rms >= 30 and speed is not None and speed >= 8)
            rows.append(dict(t_s=center, rpm=candidate if pedaling else None,
                             strength=strength, gyro_rms_dps=rms,
                             speed_kmh=speed, pedaling=pedaling))
    return rows
