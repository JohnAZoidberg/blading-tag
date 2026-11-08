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
    shows how well the integration worked), and the gait measures from
    :func:`_gait`: stance_pct, swing_s, strike_deg, toeoff_deg, impact_g,
    lift_cm, swing_dps.
    """
    cols = ["t_s", "period_s", "stride_m", "step_m", "speed_kmh", "dz_m", *GAIT_COLUMNS]
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
        pos = np.concatenate([[np.zeros(3)], np.cumsum(0.5 * (v[1:] + v[:-1]) * dt, axis=0)])
        disp = pos[-1]
        length = float(np.hypot(disp[0], disp[1]))
        gait = _gait(rs, v, pos, df["a_mag"].to_numpy()[i0:i1 + 1], df["g_mag"].to_numpy()[i0:i1 + 1], dt)
        rows.append((t[i0], period, length, length / 2, length / period * 3.6, float(disp[2]), *gait))
    return pd.DataFrame(rows, columns=cols)


GAIT_COLUMNS = ["stance_pct", "swing_s", "strike_deg", "toeoff_deg", "impact_g", "lift_cm", "swing_dps"]


def _gait(rs: np.ndarray, v: np.ndarray, pos: np.ndarray, a_mag: np.ndarray, g_mag: np.ndarray,
          dt: float) -> tuple[float, ...]:
    """Gait measures for one stride, from plant to plant (mid-stance to
    mid-stance), in the level frame of :func:`strides`.

    The foot's pitch is tracked as the angle of its forward direction (the
    direction of travel, since the foot is flat at the plant) above the
    horizontal: positive is toes up. Gyro drift over the stride is removed
    by making the pitch 0 again at the next plant.

    * Toe-off is where the foot pitches down fastest before the swing, heel
      strike the biggest |a| (the impact) after the fastest point of the
      swing. Between them is the swing; the rest of the stride is stance.
    * strike_deg: pitch at heel strike. About +15 to +25° for a heel strike,
      near 0 flat-footed, negative landing on the forefoot.
    * toeoff_deg: lowest pitch around toe-off (negative, heel up).
    * impact_g: |a| at heel strike, a rough measure of how hard the landing
      is. Sampling at 200 Hz cuts the peak short, so compare it only
      between sessions with the same rate and the same mounting.
    * lift_cm: highest point of the tag over the line between the plants.
    * swing_dps: fastest rotation in the swing.

    The angles describe the foot only when the tag sits on the shoe; on the
    shin they're the shin's angles, still fine for comparing shoes as long
    as the tag is worn the same way.
    """
    nan = (np.nan,) * len(GAIT_COLUMNS)
    n = len(v)
    horiz = np.hypot(pos[-1, 0], pos[-1, 1])
    if n < 10 or horiz < 0.2:
        return nan
    fwd = np.array([pos[-1, 0] / horiz, pos[-1, 1] / horiz, 0.0])
    # The foot's forward direction, fixed in the body frame at the plant
    # (where it's level along the direction of travel), followed through the stride.
    u = np.einsum("kij,j->ki", rs, rs[0].T @ fwd)
    pitch = np.degrees(np.unwrap(np.arctan2(u[:, 2], u @ fwd)))
    pitch -= np.linspace(0, 1, n) * pitch[-1]
    rate = np.gradient(pitch, dt)
    peak = int(np.argmax(np.hypot(v[:, 0], v[:, 1])))
    if not 2 < peak < n - 3:
        return nan
    to = int(np.argmin(rate[:peak]))
    hs = peak + int(np.argmax(a_mag[peak:]))
    swing = (hs - to) * dt
    period = (n - 1) * dt
    lo = max(0, to - int(0.1 / dt))
    toeoff = float(pitch[lo:peak].min())
    rel = pos[:, 2] - np.linspace(0, 1, n) * pos[-1, 2]
    return (100 * (1 - swing / period), swing, float(pitch[hs]), toeoff, float(a_mag[hs]),
            100 * float(rel.max()), float(g_mag[to:hs + 1].max()))


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
    g = st.dropna(subset=["strike_deg"])
    if len(g):
        m = g.median(numeric_only=True)
        lines += [f"    gait          stance {m['stance_pct']:.0f} % of the stride, swing {m['swing_s']:.2f} s;"
                  f" foot pitch {m['strike_deg']:+.0f}° at heel strike, {m['toeoff_deg']:+.0f}° at toe-off"
                  f" (+ = toes up)",
                  f"    landing       {m['impact_g']:.1f} g at heel strike; the tag lifts {m['lift_cm']:.0f} cm"
                  f" in the swing, turning at up to {m['swing_dps']:.0f} dps"]
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


# --- Comparing sessions (e.g. shoes) -----------------------------------------

COMPARED = {  # column: (label, format)
    "stride_m": ("stride m", "{:.2f}"),
    "period_s": ("period s", "{:.3f}"),
    "stance_pct": ("stance %", "{:.1f}"),
    "strike_deg": ("strike °", "{:+.1f}"),
    "toeoff_deg": ("toe-off °", "{:+.1f}"),
    "impact_g": ("impact g", "{:.2f}"),
    "lift_cm": ("lift cm", "{:.1f}"),
    "swing_dps": ("swing dps", "{:.0f}"),
}


def at_speed(st: pd.DataFrame, speed_kmh: float, band_kmh: float = 1.0, min_strides: int = 20) -> dict:
    """Each measure in :data:`COMPARED` as it would be at ``speed_kmh``.

    Almost everything in a stride changes with speed (faster means longer
    strides, a steeper heel strike, a harder landing), so two shoes worn at
    different paces would differ for that reason alone. This takes the
    strides within ``band_kmh`` of the speed, fits each measure against
    speed, and gives the median shifted to ``speed_kmh`` along that fit.
    Returns ``n`` (strides used) too; values are NaN with fewer than
    ``min_strides``.
    """
    w = st.dropna(subset=["strike_deg"])
    w = w[(w["speed_kmh"] - speed_kmh).abs() <= band_kmh]
    out = {"n": len(w), "speed_kmh": speed_kmh}
    for c in COMPARED:
        if len(w) < min_strides:
            out[c] = np.nan
            continue
        dv = w["speed_kmh"].to_numpy() - speed_kmh
        y = w[c].to_numpy()
        slope = np.polyfit(dv, y, 1)[0] if np.ptp(dv) > 0.2 else 0.0
        out[c] = float(np.median(y - slope * dv))
    return out


def compare(sessions: list[tuple[str, str, pd.DataFrame]], speed_kmh: float | None = None,
            band_kmh: float = 1.0) -> tuple[pd.DataFrame, pd.DataFrame, float]:
    """Compare walking sessions at a common speed.

    ``sessions`` is (group, name, strides) per session, e.g. group = the
    shoes. ``speed_kmh`` defaults to the median speed over all strides.
    Returns a table per session, one per group (the mean of its sessions,
    and their spread as ``sd_<column>`` when a group has more than one), and
    the speed used.

    Sessions, not strides, are the unit to compare: strides in one walk
    have the same day, route and mood in common, so a difference between
    two single walks may just be the day. Walk each pair of shoes on the
    same route more than once, ideally alternating.
    """
    if speed_kmh is None:
        allst = pd.concat([s for _, _, s in sessions]) if sessions else pd.DataFrame()
        if allst.empty:
            raise ValueError("no strides to compare")
        speed_kmh = float(allst.dropna(subset=["strike_deg"])["speed_kmh"].median())
    rows = [{"group": g, "session": n, **at_speed(s, speed_kmh, band_kmh)} for g, n, s in sessions]
    per = pd.DataFrame(rows, columns=["group", "session", "n", "speed_kmh", *COMPARED])
    agg = []
    for g, d in per.groupby("group", sort=False):
        d = d.dropna(subset=list(COMPARED), how="all")
        r = {"group": g, "sessions": len(d), "n": int(d["n"].sum())}
        r.update({c: d[c].mean() for c in COMPARED})
        if len(d) > 1:
            r.update({f"sd_{c}": d[c].std() for c in COMPARED})
        agg.append(r)
    return per, pd.DataFrame(agg), speed_kmh


def format_compare(per: pd.DataFrame, groups: pd.DataFrame, speed_kmh: float, band_kmh: float = 1.0) -> str:
    cols = list(COMPARED)
    head = ["", "walks", "strides", *(COMPARED[c][0] for c in cols)]

    def fmt(c, v):
        return "-" if pd.isna(v) else COMPARED[c][1].format(v)

    lines = [f"Walking measures at {speed_kmh:.1f} km/h (strides within {band_kmh:g} km/h,"
             f" adjusted along speed):", ""]
    table = [head]
    for _, r in groups.iterrows():
        table.append([str(r["group"]), str(r["sessions"]), str(r["n"]), *(fmt(c, r[c]) for c in cols)])
        if f"sd_{cols[0]}" in r and not pd.isna(r.get(f"sd_{cols[0]}")):
            table.append(["  ± between walks", "", "",
                          *("±" + fmt(c, r[f"sd_{c}"]).lstrip("+") for c in cols)])
    table.append([])
    table.append(["per walk"])
    for _, r in per.iterrows():
        table.append([f"  {r['group']}: {r['session']}", "", str(r["n"]), *(fmt(c, r[c]) for c in cols)])
    width = [max(len(row[i]) for row in table if len(row) > i and len(row) == len(head)) for i in range(len(head))]
    for row in table:
        if len(row) != len(head):
            lines.append(" ".join(row))
            continue
        lines.append(row[0].ljust(width[0]) + "  " + "  ".join(v.rjust(w) for v, w in zip(row[1:], width[1:])))
    lines += ["", "+ = toes up. Treat a difference as real only if it's bigger than the spread between"
              " walks in the same shoes."]
    return "\n".join(lines)
