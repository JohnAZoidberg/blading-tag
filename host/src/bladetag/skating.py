"""Skating: one row per stroke with everything worth correlating, and the
comparisons that make sense across several sessions.

``analysis.analyze`` finds the stroke cycles of the tag's foot (wheel
set-down to the next set-down). This module adds, per stroke:

* **set-down impact** (``setdown_g``): peak of the 15 Hz low-passed |a| just
  after the wheels touch down. Low-passed so road vibration (> 20 Hz) doesn't
  count; what's left is how hard the skate is put down. A soft stride sets the
  skate down at well under 2 g.
* **push-off** (``pushoff_g``): peak of the same signal at the end of the
  contact, where the leg extends and the skate leaves the road. This is the
  "kick" at the end of a push, the hardest moment in the cycle.
* **swing** (``swing_dps``): peak rotation rate while the skate is in the
  air, i.e. how fast the recovery is.
* **road** (``road_vib_g``): median vibration RMS above 20 Hz while rolling,
  a measure of the surface (and of speed: vibration rises with speed).
* **GPS**: speed, heading, position and the speed change, interpolated to
  the middle of the stroke.
* **foot path** (from ``viz3d.stroke_motion``): how far out the foot goes,
  push speed, edge and toe-out angle, from the tag's integrated motion.

Across sessions:

* ``correlations``: Spearman's rank correlation of every pair of measures,
  both pooled and *within session* (each session's median removed first).
  Pooled correlations mix in whatever differs between sessions (wind, foot,
  fatigue, surface); within-session ones are what a stroke-to-stroke change
  does.
* ``at_speed``: each measure moved to one common speed along its fit against
  speed, so sessions at different speeds can be compared.
* ``route_pairs``: strokes from two sessions at the same place on the map
  (within ``radius_m``), going the same or the opposite way. Out-and-back on
  one road gives the same surface and gradient both ways, so what's left is
  wind, fatigue and which foot carries the tag.
* ``coasts``: stretches where the GPS says rolling but the tag sees no
  strokes. The speed lost while coasting is rolling resistance plus air drag;
  air drag depends on the wind, so the same coast at the same speed loses
  speed faster into a headwind.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import signal

from .analysis import Params as AnalysisParams
from .analysis import Result, analyze
from .session import Session, load_session

EARTH_R_M = 6_371_000.0


@dataclass(frozen=True)
class Measure:
    key: str
    label: str
    unit: str
    fmt: str = "{:.2f}"
    about: str = ""


#: Every per-stroke measure the report knows about, in display order.
MEASURES: list[Measure] = [
    Measure("speed_kmh", "Speed", "km/h", "{:.1f}", "GPS speed in the middle of the stroke"),
    Measure("push_rate_spm", "Push rate", "/min", "{:.1f}", "pushes per minute, both feet (2x this foot)"),
    Measure("dist_per_push_m", "Distance per push", "m", "{:.2f}", "speed x half the cycle"),
    Measure("period_s", "Cycle", "s", "{:.2f}", "set-down to set-down of this foot"),
    Measure("contact_frac", "Contact", "", "{:.0%}", "share of the cycle with the wheels on the road"),
    Measure("contact_s", "Contact time", "s", "{:.2f}", "wheels on the road per cycle"),
    Measure("air_s", "Air time", "s", "{:.2f}", "skate in the air per cycle (recovery)"),
    Measure("setdown_g", "Set-down impact", "g", "{:.2f}", "how hard the skate lands (15 Hz low-passed |a|)"),
    Measure("pushoff_g", "Push-off peak", "g", "{:.2f}", "the kick at the end of the push (15 Hz low-passed |a|)"),
    Measure("swing_dps", "Swing speed", "°/s", "{:.0f}", "peak rotation rate during the recovery"),
    Measure("road_vib_g", "Road vibration", "g", "{:.2f}", "RMS above 20 Hz while rolling: surface roughness"),
    Measure("accel_kmh_s", "Speed change", "km/h/s", "{:.2f}", "GPS speed slope around the stroke"),
    Measure("out_cm", "Outward reach", "cm", "{:.0f}", "how far out the foot goes from set-down to its widest point (3D foot path)"),
    Measure("sweep_cm", "Sideways sweep", "cm", "{:.0f}", "sideways range of the foot relative to the skater, body sway included (3D foot path)"),
    Measure("push_speed_mps", "Push speed", "m/s", "{:.2f}", "peak outward speed of the skate while the wheels are down (3D foot path)"),
    Measure("edge_deg", "Edge angle", "°", "{:.0f}", "tilt onto the inside edge, 95th pct while wheels down (3D foot path)"),
    Measure("outside_deg", "Outside edge", "°", "{:.0f}", "tilt onto the outside edge, 95th pct while wheels down; the double-push set-down (3D foot path)"),
    Measure("outside_frac", "On outside edge", "", "{:.0%}", "share of the contact more than 4° onto the outside edge; double push when high (3D foot path)"),
    Measure("splay_deg", "Toe-out", "°", "{:.1f}", "skate turned out from the direction of travel (3D foot path)"),
    Measure("fore_aft_cm", "Fore-aft", "cm", "{:.0f}", "forward-backward range of the foot relative to the skater (3D foot path)"),
    Measure("lift_cm", "Lift", "cm", "{:.1f}", "recovery height; only resolves 'stays very low' (3D foot path)"),
]
MEASURE = {m.key: m for m in MEASURES}


@dataclass
class Params:
    impact_lp_hz: float = 15.0      # below the road vibration band
    setdown_win_s: tuple[float, float] = (-0.03, 0.10)
    pushoff_win_s: tuple[float, float] = (-0.20, 0.05)
    min_speed_kmh: float = 5.0      # strokes slower than this are starts and stops
    gps_slope_s: float = 3.0        # speed change from a fit over +- this many seconds


def _peak_in(x: np.ndarray, t: np.ndarray, centres: np.ndarray, win: tuple[float, float]) -> np.ndarray:
    lo = np.searchsorted(t, centres + win[0])
    hi = np.searchsorted(t, centres + win[1])
    out = np.full(len(centres), np.nan)
    for k, (a, b) in enumerate(zip(lo, hi)):
        if b > a and np.isfinite(centres[k]):
            out[k] = x[a:b].max()
    return out


def _gps_slope(gps: pd.DataFrame, t: np.ndarray, half_s: float) -> np.ndarray:
    gt, gv = gps["t_s"].to_numpy(), gps["speed_mps"].to_numpy() * 3.6
    out = np.full(len(t), np.nan)
    for k, tc in enumerate(t):
        m = (gt >= tc - half_s) & (gt <= tc + half_s) & np.isfinite(gv)
        if m.sum() >= 3:
            out[k] = np.polyfit(gt[m] - tc, gv[m], 1)[0]
    return out


def stroke_table(sess: Session, result: Result | None = None, p: Params | None = None,
                 foot_path: bool = True, up_hint: tuple | None = None) -> pd.DataFrame:
    """One row per stroke cycle of the session's first tag (or ``result``'s).
    ``up_hint``: "flat" for the edge angles, see ``viz3d.shared_rest_up``."""
    p = p or Params()
    r = result or analyze(sess)
    st = r.strokes.copy()
    if st.empty:
        return st
    sig, tag = r.signals, r.tag
    fs = r.clock.rate_hz
    lp = np.zeros(len(sig.t))
    sos = signal.butter(2, p.impact_lp_hz, fs=fs, output="sos")
    acc = tag.accel_g
    for s in np.unique(sig.segment):
        m = sig.segment == s
        if m.sum() > 30:
            lp[m] = np.linalg.norm(signal.sosfiltfilt(sos, acc[m], axis=0), axis=1)

    setdown = st["set_down_s"].fillna(st["t_s"]).to_numpy()
    st["setdown_g"] = _peak_in(lp, sig.t, setdown, p.setdown_win_s)
    st["pushoff_g"] = _peak_in(lp, sig.t, st["lift_off_s"].to_numpy(), p.pushoff_win_s)

    i0 = np.searchsorted(sig.t, st["t_s"].to_numpy())
    i1 = np.searchsorted(sig.t, (st["t_s"] + st["period_s"]).to_numpy())
    swing, road = np.full(len(st), np.nan), np.full(len(st), np.nan)
    for k, (a, b) in enumerate(zip(i0, i1)):
        c = sig.contact[a:b]
        if (~c).any():
            swing[k] = sig.gyro_lp[a:b][~c].max()
        if c.any():
            road[k] = np.median(sig.vib[a:b][c])
    st["swing_dps"] = swing
    st["road_vib_g"] = road

    st["speed_kmh"] = st["speed_mps"] * 3.6
    gps = r.gps
    mid = (st["t_s"] + st["period_s"] / 2).to_numpy()
    if gps is not None and len(gps) > 2:
        g = gps.dropna(subset=["lat", "lon"])
        st["lat"] = np.interp(mid, g["t_s"], g["lat"])
        st["lon"] = np.interp(mid, g["t_s"], g["lon"])
        # Bearing is circular: interpolate its unit vector.
        b = np.deg2rad(g["bearing_deg"].to_numpy())
        ok = np.isfinite(b) & (g["speed_mps"].to_numpy() > 1.5)
        if ok.sum() > 2:
            bx = np.interp(mid, g["t_s"][ok], np.sin(b[ok]))
            by = np.interp(mid, g["t_s"][ok], np.cos(b[ok]))
            st["bearing_deg"] = np.rad2deg(np.arctan2(bx, by)) % 360
        else:
            st["bearing_deg"] = np.nan
        st["gps_acc_m"] = np.interp(mid, g["t_s"], g["h_acc_m"])
        st["accel_kmh_s"] = _gps_slope(gps, mid, p.gps_slope_s)
    else:
        for c in ("lat", "lon", "bearing_deg", "gps_acc_m", "accel_kmh_s"):
            st[c] = np.nan

    if foot_path:
        _join_foot_path(st, sess, r, up_hint)
    for m in MEASURES:
        if m.key not in st:
            st[m.key] = np.nan
    st["foot"] = tag.meta.position
    st["t_min"] = st["t_s"] / 60
    return st


FOOT_PATH = ["out_cm", "sweep_cm", "push_speed_mps", "edge_deg", "outside_deg", "outside_frac", "splay_deg", "fore_aft_cm", "lift_cm"]


def _join_foot_path(st: pd.DataFrame, sess: Session, r: Result, up_hint: tuple | None = None) -> None:
    """Outward reach, push speed, edge and toe-out from the 3D foot path
    (``viz3d.stroke_motion``), joined on the stroke's row in ``r.strokes``."""
    try:
        from .viz3d import foot_motion
    except ImportError:
        return
    try:
        fm = foot_motion(sess, r.tag, result=r, up_hint=up_hint).strokes
    except Exception as e:  # the foot path is extra; never lose the rest over it
        st.attrs["foot_path_error"] = f"foot path: {type(e).__name__}: {e}"
        return
    if fm is None or fm.empty or "stroke" not in fm:
        return
    fm = fm.set_index("stroke")
    for k in FOOT_PATH:
        if k in fm:
            st[k] = fm[k].reindex(st.index)


# --- many sessions -------------------------------------------------------------


@dataclass
class SessionInfo:
    key: str           # short label, e.g. "11:49"
    path: str
    start_utc: str
    activity: str
    gear: str
    notes: str
    foot: str
    summary: dict
    warnings: list


def load_sessions(paths: list[str | Path], p: Params | None = None, activity: str = "skating",
                  progress=None, labelled_only: set | None = None) -> tuple[pd.DataFrame, list[SessionInfo]]:
    """Stroke tables of every skating session in ``paths``, concatenated, with
    a ``session`` column. Sessions of another activity are skipped.
    ``unknown`` ones are taken (old recordings) unless they're in
    ``labelled_only``."""
    p = p or Params()
    frames, infos, coast_frames = [], [], []
    items = []
    for path in paths:
        sess = load_session(path)
        if activity and sess.activity != activity and (
                sess.activity != "unknown" or (labelled_only and path in labelled_only)):
            continue
        items += [(path, sess, tag) for tag in sess.tags
                  if "foot" in tag.meta.position or tag.meta.position == "unknown"]
    # "Flat" for the edge angles, shared by the day's sessions with the same
    # tag on the same foot, as on the viz3d page.
    try:
        from .viz3d import shared_rest_up
        hints = shared_rest_up([(sess, tag) for _, sess, tag in items])
    except Exception:  # the foot path is extra
        hints = {}
    for i, (path, sess, tag) in enumerate(items):
        if progress:
            progress(f"{Path(path).name} {tag.meta.position}")
        try:
            r = analyze(sess, tag)
        except ValueError as e:
            if progress:
                progress(f"  skipped: {e}")
            continue
        st = stroke_table(sess, r, p, up_hint=hints.get(i))
        if st.empty:
            continue
        key = _session_key(sess, len(sess.tags) > 1, tag.meta.position)
        if any(inf.key == key for inf in infos):  # two sessions started in the same minute
            key = f"{key} ({Path(path).name})"
        st.insert(0, "session", key)
        frames.append(st)
        co = coasts(r)
        if len(co):
            co.insert(0, "session", key)
            coast_frames.append(co)
        sm = {k: v for k, v in r.summary.items() if k != "drift"}
        infos.append(SessionInfo(key, str(path), sess.meta.get("start_utc", ""), sess.activity,
                                 sess.gear, sess.meta.get("notes") or "", tag.meta.position, sm,
                                 r.warnings + ([st.attrs["foot_path_error"]] if "foot_path_error" in st.attrs else [])))
    if not frames:
        return pd.DataFrame(), infos
    df = pd.concat(frames, ignore_index=True)
    df["moving"] = df["speed_kmh"].fillna(np.inf) >= p.min_speed_kmh
    df.attrs["coasts"] = pd.concat(coast_frames, ignore_index=True) if coast_frames else pd.DataFrame()
    return df, infos


def _session_key(sess: Session, multi: bool, position: str) -> str:
    try:
        key = sess.start_utc.astimezone().strftime("%m-%d %H:%M")
    except (KeyError, ValueError):
        key = Path(sess.path).name
    return f"{key} {position}" if multi else key


def measures_present(df: pd.DataFrame) -> list[str]:
    return [m.key for m in MEASURES if m.key in df and df[m.key].notna().sum() >= 10]


def _spearman(x: np.ndarray, y: np.ndarray) -> float:
    ok = np.isfinite(x) & np.isfinite(y)
    if ok.sum() < 10:
        return float("nan")
    rx = pd.Series(x[ok]).rank().to_numpy()
    ry = pd.Series(y[ok]).rank().to_numpy()
    return float(np.corrcoef(rx, ry)[0, 1])


def correlations(df: pd.DataFrame, keys: list[str]) -> dict[str, pd.DataFrame]:
    """Spearman correlation matrices: ``pooled`` over all strokes and
    ``within`` with each session's median removed first."""
    d = df[df["moving"]]
    within = d[keys] - d.groupby("session")[keys].transform("median")
    out = {}
    for name, frame in (("pooled", d[keys]), ("within", within)):
        m = pd.DataFrame(np.nan, index=keys, columns=keys)
        for i, a in enumerate(keys):
            for b in keys[i:]:
                v = 1.0 if a == b else _spearman(frame[a].to_numpy(float), frame[b].to_numpy(float))
                m.loc[a, b] = m.loc[b, a] = v
        out[name] = m
    return out


def speed_slopes(df: pd.DataFrame, keys: list[str]) -> dict[str, float]:
    """Within-session slope of each measure against speed (per km/h), pooled
    over sessions: the session medians are removed first, so differences
    between sessions don't tilt the fit."""
    d = df[df["moving"]].dropna(subset=["speed_kmh"])
    out = {}
    for k in keys:
        if k == "speed_kmh":
            continue
        e = d.dropna(subset=[k])
        if len(e) < 20:
            continue
        x = e["speed_kmh"] - e.groupby("session")["speed_kmh"].transform("median")
        y = e[k] - e.groupby("session")[k].transform("median")
        lo, hi = y.quantile([0.02, 0.98])
        m = (y >= lo) & (y <= hi)
        if x[m].std() > 0:
            out[k] = float(np.polyfit(x[m], y[m], 1)[0])
    return out


def at_speed(df: pd.DataFrame, keys: list[str], speed_kmh: float | None = None) -> tuple[pd.DataFrame, float]:
    """Per-session median of every measure moved to one common speed along the
    within-session slope against speed. Returns (table, speed)."""
    d = df[df["moving"]].dropna(subset=["speed_kmh"])
    speed = float(d["speed_kmh"].median()) if speed_kmh is None else speed_kmh
    slopes = speed_slopes(df, keys)
    rows = {}
    for sess, g in d.groupby("session", sort=False):
        row = {"strokes": len(g), "speed_kmh": float(g["speed_kmh"].median())}
        for k in keys:
            if k == "speed_kmh":
                continue
            adj = g[k] + slopes.get(k, 0.0) * (speed - g["speed_kmh"])
            row[k] = float(adj.median()) if adj.notna().any() else np.nan
        rows[sess] = row
    return pd.DataFrame(rows).T, speed


def _xy_m(lat: np.ndarray, lon: np.ndarray, lat0: float, lon0: float) -> np.ndarray:
    x = np.deg2rad(lon - lon0) * np.cos(np.deg2rad(lat0)) * EARTH_R_M
    y = np.deg2rad(lat - lat0) * EARTH_R_M
    return np.column_stack([x, y])


def route_pairs(df: pd.DataFrame, radius_m: float = 15.0, keys: list[str] | None = None) -> pd.DataFrame:
    """Every pair of sessions that cover the same ground, compared at the same
    places. One row per (session A, session B, direction): how many matched
    places, the median of each measure in A and in B there, and whether they
    went the same way or opposite ways."""
    d = df[df["moving"]].dropna(subset=["lat", "lon"])
    if d.empty:
        return pd.DataFrame()
    keys = keys or measures_present(d)
    lat0, lon0 = d["lat"].median(), d["lon"].median()
    xy = _xy_m(d["lat"].to_numpy(), d["lon"].to_numpy(), lat0, lon0)
    sess = d["session"].to_numpy()
    bear = d["bearing_deg"].to_numpy()
    from scipy.spatial import cKDTree

    names = list(dict.fromkeys(sess))
    rows = []
    for i, a in enumerate(names):
        ia = np.flatnonzero(sess == a)
        for b in names[i + 1:]:
            ib = np.flatnonzero(sess == b)
            tree = cKDTree(xy[ib])
            dist, j = tree.query(xy[ia], distance_upper_bound=radius_m)
            ok = np.isfinite(dist)
            if ok.sum() < 20:
                continue
            pa, pb = ia[ok], ib[j[ok]]
            diff = np.abs((bear[pa] - bear[pb] + 180) % 360 - 180)
            for way, m in (("same way", diff < 45), ("opposite", diff > 135)):
                if m.sum() < 20:
                    continue
                row = {"a": a, "b": b, "direction": way, "places": int(m.sum()),
                       "overlap_m": float(_path_len(xy[pa[m]]))}
                for k in keys:
                    va, vb = d[k].to_numpy()[pa[m]], d[k].to_numpy()[pb[m]]
                    row[f"{k}_a"] = float(np.nanmedian(va)) if np.isfinite(va).any() else np.nan
                    row[f"{k}_b"] = float(np.nanmedian(vb)) if np.isfinite(vb).any() else np.nan
                rows.append(row)
    return pd.DataFrame(rows)


def _path_len(xy: np.ndarray) -> float:
    """Rough length of the ground covered by these points (5 m grid cells)."""
    if len(xy) < 2:
        return 0.0
    return float(len(np.unique(np.round(xy / 5.0), axis=0)) * 5.0)


def coasts(r: Result, min_s: float = 3.0, min_kmh: float = 8.0) -> pd.DataFrame:
    """Stretches of at least ``min_s`` where the GPS says rolling above
    ``min_kmh`` but the tag's foot isn't moving (no strokes): how fast the
    skater slows down there, in km/h per second."""
    if r.gps is None:
        return pd.DataFrame()
    sig, gps = r.signals, r.gps
    fs = r.clock.rate_hz
    idle = ~sig.moving
    d = np.diff(np.concatenate([[0], idle.astype(np.int8), [0]]))
    rows = []
    for a, b in zip(np.flatnonzero(d == 1), np.flatnonzero(d == -1)):
        if (b - a) / fs < min_s:
            continue
        t0, t1 = sig.t[a] + 0.5, sig.t[b - 1] - 0.5
        g = gps[(gps["t_s"] >= t0) & (gps["t_s"] <= t1)].dropna(subset=["speed_mps"])
        if len(g) < 3 or g["speed_mps"].min() * 3.6 < min_kmh:
            continue
        slope = np.polyfit(g["t_s"], g["speed_mps"] * 3.6, 1)[0]
        b_ = np.deg2rad(g["bearing_deg"].to_numpy())
        rows.append({"t_s": t0, "duration_s": t1 - t0, "speed_kmh": float(g["speed_mps"].mean() * 3.6),
                     "decel_kmh_s": float(-slope),
                     "bearing_deg": float(np.rad2deg(np.arctan2(np.sin(b_).mean(), np.cos(b_).mean())) % 360),
                     "lat": float(g["lat"].mean()), "lon": float(g["lon"].mean())})
    return pd.DataFrame(rows)
