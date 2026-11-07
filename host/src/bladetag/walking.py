"""Walking: stride length from the tag alone.

While the foot is planted, the ankle is (nearly) still. Between two such
moments the tag's acceleration is rotated into a level frame with the gyro,
gravity is removed and it's integrated twice. The velocity must be zero
again at the next plant, so whatever is left there is integration error and
is removed linearly over the stride (a "zero-velocity update"). That cancels
most of the drift, so a stride comes out within a few percent. On the first
real walks (tag on the right ankle) this agreed with GPS to 1 % where GPS
was good (±4 m).

A stride is from one plant of the tag's foot to the next, i.e. two steps.
The step length given is half a stride: one tag can't tell how a stride
splits between the feet.

This is for walking (and running) only. On skates the wheels keep rolling
while the foot is down, so there is no zero-velocity moment. Gliding looks
like one (steady orientation, |a| near 1 g) but carries road vibration, which
a planted shoe doesn't: plants with more than ``stance_vib_g`` above 20 Hz
are rejected, so a skating session finds no strides rather than wrong ones.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from .explore import ACCEL, GYRO, _runs, band_rms, still_mask

G = 9.80665


@dataclass
class Params:
    stance_gyro_dps: float = 60.0  # |gyro| below this over stance_win_s ...
    stance_acc_g: float = 0.25     # ... and |a| within this of 1 g counts as planted
    stance_win_s: float = 0.06
    stance_vib_g: float = 0.1      # RMS above 20 Hz: planted shoes ~0.02 g, gliding skates ~0.3 g
    min_period_s: float = 0.7      # walking strides (one foot) take about 0.9-1.5 s
    max_period_s: float = 1.8


def _skew(v: np.ndarray) -> np.ndarray:
    """(..., 3) -> (..., 3, 3) cross-product matrices."""
    k = np.zeros(v.shape[:-1] + (3, 3))
    k[..., 0, 1], k[..., 0, 2] = -v[..., 2], v[..., 1]
    k[..., 1, 0], k[..., 1, 2] = v[..., 2], -v[..., 0]
    k[..., 2, 0], k[..., 2, 1] = -v[..., 1], v[..., 0]
    return k


def _rotations(w: np.ndarray) -> np.ndarray:
    """Rotation vectors (m, 3) in rad -> rotation matrices (m, 3, 3)."""
    th = np.linalg.norm(w, axis=1)[:, None, None]
    k = _skew(w / np.maximum(th[..., 0], 1e-12))
    return np.eye(3) + np.sin(th) * k + (1 - np.cos(th)) * (k @ k)


def _level(a: np.ndarray) -> np.ndarray:
    """A rotation taking the measured specific force ``a`` (pointing up at
    rest) onto +z. Heading is arbitrary, which doesn't matter for a length."""
    a = a / np.linalg.norm(a)
    z = np.array([0.0, 0.0, 1.0])
    v, c = np.cross(a, z), float(a @ z)
    s = np.linalg.norm(v)
    if s < 1e-9:
        return np.eye(3) if c > 0 else np.diag([1.0, -1.0, -1.0])
    k = _skew(v)
    return np.eye(3) + k + k @ k * ((1 - c) / s**2)


def stance_anchors(df: pd.DataFrame, fs: float, still: np.ndarray | None = None,
                   p: Params = Params()) -> np.ndarray:
    """Sample index of the quietest moment of each stance (foot planted).
    Rest periods are left out: they aren't part of a stride. So are "plants"
    with road vibration: that's a skate gliding, not a foot standing."""
    still = still_mask(df, fs) if still is None else still
    w = max(3, int(p.stance_win_s * fs))
    gm = pd.Series(df["g_mag"].to_numpy()).rolling(w, center=True).max().to_numpy()
    ad = pd.Series(np.abs(df["a_mag"].to_numpy() - 1)).rolling(w, center=True).max().to_numpy()
    stance = (gm < p.stance_gyro_dps) & (ad < p.stance_acc_g) & ~still
    rs, re = _runs(stance)
    anchors = np.array([i + int(np.argmin(gm[i:j])) for i, j in zip(rs, re) if j - i >= 2], dtype=int)
    if not len(anchors):
        return anchors
    vib = pd.Series(band_rms(df, fs) ** 2).rolling(w, center=True, min_periods=1).mean().to_numpy() ** 0.5
    return anchors[vib[anchors] < p.stance_vib_g]


def strides(df: pd.DataFrame, fs: float, still: np.ndarray | None = None, p: Params = Params()) -> pd.DataFrame:
    """One row per stride of the tag's foot.

    Columns: t_s (the plant it starts at), period_s, stride_m (horizontal
    distance to the next plant), step_m (half of that), speed_kmh, dz_m
    (height change over the stride; should be about 0 on the flat, so it
    shows how well the integration worked).
    """
    cols = ["t_s", "period_s", "stride_m", "step_m", "speed_kmh", "dz_m"]
    if len(df) < 2:
        return pd.DataFrame(columns=cols)
    still = still_mask(df, fs) if still is None else still
    t = df["t_s"].to_numpy()
    seg = df["segment"].to_numpy()
    acc = df[ACCEL].to_numpy() * G
    gyr = df[GYRO].to_numpy()
    bias = gyr[still].mean(axis=0) if still.any() else np.zeros(3)
    gyr = np.radians(gyr - bias)
    dt = 1 / fs
    anchors = stance_anchors(df, fs, still, p)
    rows = []
    for i0, i1 in zip(anchors[:-1], anchors[1:]):
        period = t[i1] - t[i0]
        if not (p.min_period_s < period < p.max_period_s) or seg[i0] != seg[i1]:
            continue
        rs = np.empty((i1 - i0 + 1, 3, 3))
        rs[0] = _level(acc[max(0, i0 - 3):i0 + 4].mean(axis=0))
        d = _rotations(0.5 * (gyr[i0:i1] + gyr[i0 + 1:i1 + 1]) * dt)
        for k in range(i1 - i0):
            rs[k + 1] = rs[k] @ d[k]
        aw = np.einsum("kij,kj->ki", rs, acc[i0:i1 + 1]) - [0.0, 0.0, G]
        v = np.concatenate([[np.zeros(3)], np.cumsum(0.5 * (aw[1:] + aw[:-1]) * dt, axis=0)])
        v -= np.outer(np.linspace(0, 1, len(v)), v[-1])
        disp = (0.5 * (v[1:] + v[:-1]) * dt).sum(axis=0)
        length = float(np.hypot(disp[0], disp[1]))
        rows.append((t[i0], period, length, length / 2, length / period * 3.6, float(disp[2])))
    return pd.DataFrame(rows, columns=cols)


def describe(st: pd.DataFrame, gps: pd.DataFrame | None = None) -> list[str]:
    """Summary lines in the style of ``explore.describe``."""
    if st.empty:
        return ["  strides         none found (walking only: needs the foot planted still at each step)"]
    q1, med, q3 = st["stride_m"].quantile([0.25, 0.5, 0.75])
    lines = [f"  strides         {len(st)}, by integrating between foot plants (walking only, not skating)",
             f"    length        {med:.2f} m median ({q1:.2f}-{q3:.2f}), so about {med / 2:.2f} m per step;"
             f" {60 / st['period_s'].median():.1f} strides/min",
             f"    speed         {st['speed_kmh'].median():.1f} km/h median, {st['stride_m'].sum():.0f} m in strides;"
             f" height error {100 * st['dz_m'].median():+.1f} cm per stride (ideally 0)"]
    if gps is not None and len(gps) >= 2:
        a, b = st["t_s"].to_numpy(), (st["t_s"] + st["period_s"]).to_numpy()
        m = (a >= gps["t_s"].iloc[0]) & (b <= gps["t_s"].iloc[-1])
        if m.any():
            d_gps = (np.interp(b[m], gps["t_s"], gps["dist_m"]) - np.interp(a[m], gps["t_s"], gps["dist_m"])).sum()
            g = gps[(gps["t_s"] >= a[m].min()) & (gps["t_s"] <= b[m].max())]
            acc = f" (GPS accuracy ±{g['h_acc_m'].median():.0f} m median)" if len(g) and "h_acc_m" in g else ""
            lines.append(f"    vs GPS        {st['stride_m'][m].sum():.0f} m by the tag, {d_gps:.0f} m by GPS speed"
                         f" over the same strides{acc}")
    return lines
