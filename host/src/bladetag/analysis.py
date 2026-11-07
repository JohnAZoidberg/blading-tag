"""Analysis v1 (PLAN.md phase 6): stroke cycles, push rate, contact/air
split, distance per push and fatigue drift for one tag.

Mount-independent on purpose, so it works before the mounting orientation is
worked out:

* **Stroke cycles** come from the recovery swing: the skate rotates most
  while it is in the air, so low-passed |gyro| peaks once per cycle of
  this foot.
* **Ground contact** comes from road vibration: high-frequency acceleration
  energy is only there while the wheels touch the road. The threshold is
  found per block (Otsu on the log envelope), so it adapts to the surface.
* **Speed and distance** come from the phone's GPS Doppler speed.

One tag sees one foot. Skaters alternate feet, so the total push rate is
estimated as twice this foot's cycle rate.

The thresholds were set on simulated data (bladetag.sim) and still need
checking against real recordings.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd
from scipy import ndimage, signal

from .session import Session, TagData
from .timesync import ClockFit, fit_clocks


@dataclass
class Params:
    cadence_lp_hz: float = 4.0        # low-pass for |gyro| before peak picking
    moving_gyro_dps: float = 30.0     # 1 s mean |gyro| above this = skating
    min_period_s: float = 0.6
    max_period_s: float = 3.0
    vib_hp_hz: float = 20.0           # road vibration band starts here
    vib_win_s: float = 0.04           # RMS window for the vibration envelope
    vib_block_s: float = 60.0         # adaptive threshold block length
    min_contact_s: float = 0.10
    min_air_s: float = 0.08
    fatigue_frac: float = 0.2         # compare first vs last 20 % of the session


@dataclass
class Signals:
    """Per-sample signals on the session time base (seconds since start_utc)."""
    t: np.ndarray
    gyro_mag: np.ndarray       # dps
    gyro_lp: np.ndarray        # dps, low-passed
    vib: np.ndarray            # g RMS, road-vibration envelope
    contact: np.ndarray        # bool
    moving: np.ndarray         # bool
    segment: np.ndarray        # int, contiguous-sample segment id
    vib_threshold: np.ndarray  # g, per sample
    vib_separation: float      # how bimodal the vibration is; < ~1.5 means unreliable


@dataclass
class Result:
    tag: TagData
    clock: ClockFit
    signals: Signals
    strokes: pd.DataFrame
    gps: pd.DataFrame | None
    summary: dict
    per_km: pd.DataFrame | None
    warnings: list[str] = field(default_factory=list)


# --- helpers -----------------------------------------------------------------


def _segments(sample_index: np.ndarray) -> np.ndarray:
    brk = np.concatenate([[0], np.diff(sample_index) != 1]).astype(bool)
    return np.cumsum(brk)


def _filt_segments(sos, x: np.ndarray, seg: np.ndarray) -> np.ndarray:
    out = np.zeros_like(x, dtype=np.float64)
    pad = 3 * (2 * len(sos) + 1)
    for s in np.unique(seg):
        m = seg == s
        if m.sum() > pad + 1:
            out[m] = signal.sosfiltfilt(sos, x[m], axis=0)
    return out


def _moving_mean(x: np.ndarray, n: int) -> np.ndarray:
    n = max(1, int(n))
    k = np.ones(n) / n
    return np.convolve(x, k, mode="same")


def _otsu(x: np.ndarray, bins: int = 64) -> tuple[float, float]:
    """Threshold and separation (between-class distance / pooled std)."""
    x = x[np.isfinite(x)]
    if len(x) < 10:
        return float("nan"), 0.0
    hist, edges = np.histogram(x, bins)
    mid = (edges[:-1] + edges[1:]) / 2
    w0 = np.cumsum(hist)
    w1 = w0[-1] - w0
    s0 = np.cumsum(hist * mid)
    m0 = s0 / np.maximum(w0, 1)
    m1 = (s0[-1] - s0) / np.maximum(w1, 1)
    between = w0 * w1 * (m0 - m1) ** 2
    k = int(np.argmax(between[:-1]))
    thr = edges[k + 1]
    lo, hi = x[x <= thr], x[x > thr]
    if len(lo) < 2 or len(hi) < 2:
        return thr, 0.0
    pooled = np.sqrt((lo.var() * len(lo) + hi.var() * len(hi)) / len(x))
    return float(thr), float((hi.mean() - lo.mean()) / max(pooled, 1e-9))


def _clean_runs(mask: np.ndarray, min_true: int, min_false: int) -> np.ndarray:
    """Fill short False gaps, then remove short True runs."""
    m = mask.copy()
    for val, min_len in ((False, min_false), (True, min_true)):
        d = np.diff(np.concatenate([[not val], m == val, [not val]]).astype(int))
        starts, ends = np.flatnonzero(d == 1), np.flatnonzero(d == -1)
        for a, b in zip(starts, ends):
            if b - a < min_len:
                m[a:b] = not val
    return m


def _estimate_period(x: np.ndarray, fs: float, p: Params) -> float | None:
    x = x - x.mean()
    if len(x) < fs * p.max_period_s * 3:
        return None
    nfft = 1 << int(np.ceil(np.log2(2 * len(x))))
    f = np.fft.rfft(x, nfft)
    ac = np.fft.irfft(f * np.conj(f))[: len(x)]
    lo, hi = int(p.min_period_s * fs), int(p.max_period_s * fs)
    lag = lo + int(np.argmax(ac[lo:hi]))
    return lag / fs if ac[lag] > 0 else None


# --- main steps --------------------------------------------------------------


def compute_signals(sess: Session, tag: TagData, clock: ClockFit, p: Params) -> Signals:
    fs = clock.rate_hz
    t = (clock.sample_elapsed_ns(tag.sample_index) - sess.start_elapsed_ns) / 1e9
    seg = _segments(tag.sample_index)
    acc, gyr = tag.accel_g, tag.gyro_dps
    gmag = np.linalg.norm(gyr, axis=1)
    glp = _filt_segments(signal.butter(2, p.cadence_lp_hz, fs=fs, output="sos"), gmag, seg)
    moving = _moving_mean(glp, fs) > p.moving_gyro_dps

    hp = _filt_segments(signal.butter(4, p.vib_hp_hz, "highpass", fs=fs, output="sos"), acc, seg)
    vib = np.sqrt(_moving_mean((hp ** 2).sum(axis=1), p.vib_win_s * fs))
    logv = np.log10(vib + 1e-4)
    thr = np.full(len(t), np.nan)
    seps = []
    block = int(p.vib_block_s * fs)
    for b0 in range(0, len(t), block):
        sl = slice(b0, min(len(t), b0 + block))
        sel = moving[sl]
        if sel.sum() < fs * 5:
            continue
        th, sep = _otsu(logv[sl][sel])
        thr[sl] = th
        seps.append(sep)
    # Blocks without enough skating borrow the nearest threshold.
    if np.isfinite(thr).any():
        idx = np.arange(len(t))
        ok = np.isfinite(thr)
        thr = np.interp(idx, idx[ok], thr[ok])
    contact = (logv > thr) & moving
    # The centred RMS window widens each contact by half a window per side.
    half = int(p.vib_win_s * fs / 2)
    if half:
        contact = ndimage.binary_erosion(contact, np.ones(2 * half + 1, bool))
    contact = _clean_runs(contact, int(p.min_contact_s * fs), int(p.min_air_s * fs))
    return Signals(t, gmag, glp, vib, contact, moving, seg, 10 ** thr,
                   float(np.median(seps)) if seps else 0.0)


def _gyro_boundaries(sig: Signals, fs: float, p: Params) -> np.ndarray:
    """One |gyro| peak per cycle (the recovery swing). Fallback when road
    vibration can't be trusted: |gyro| often has a second, smaller hump
    during the push, so which hump wins can flip."""
    x = sig.gyro_lp
    period = _estimate_period(x[sig.moving], fs, p)
    dist = int((period or p.min_period_s) * 0.55 * fs)
    ref = np.percentile(x[sig.moving], 90) if sig.moving.any() else 1.0
    peaks, _ = signal.find_peaks(x, distance=max(1, dist), prominence=0.25 * ref)
    return peaks[sig.moving[peaks]]


def _setdown_boundaries(sig: Signals) -> np.ndarray:
    """Wheels touching down (air -> contact): a sharp, physical event."""
    return np.flatnonzero(np.diff(sig.contact.astype(np.int8)) == 1) + 1


def detect_strokes(sig: Signals, fs: float, p: Params, method: str = "auto") -> tuple[pd.DataFrame, str]:
    """Split the signal into stroke cycles of this foot.

    method: "setdown" (vibration), "gyro", or "auto": set-down when it agrees
    with the gyro cadence, which is a better test of whether the vibration
    signal works than its bimodality alone.
    """
    if method == "auto":
        gy, _ = detect_strokes(sig, fs, p, "gyro")
        sd, _ = detect_strokes(sig, fs, p, "setdown")
        if (sig.vib_separation >= 1.5 and len(sd) >= 0.8 * len(gy) and len(sd)
                and abs(sd["period_s"].median() / gy["period_s"].median() - 1) < 0.1):
            return sd, "setdown"
        return gy, "gyro"
    bounds = _setdown_boundaries(sig) if method == "setdown" else _gyro_boundaries(sig, fs, p)

    x = sig.gyro_lp
    rows = []
    for a, b in zip(bounds[:-1], bounds[1:]):
        if sig.segment[a] != sig.segment[b]:
            continue
        dur = sig.t[b] - sig.t[a]
        if not (p.min_period_s <= dur <= p.max_period_s) or not sig.moving[a:b].all():
            continue
        c = sig.contact[a:b]
        dc = np.diff(c.astype(np.int8))
        downs, ups = np.flatnonzero(dc == 1) + 1, np.flatnonzero(dc == -1) + 1
        has_contact = bool(c.any())
        contact_s = c.sum() / fs
        rows.append({
            "t_s": sig.t[a],
            "period_s": dur,
            "contact_s": contact_s if has_contact else np.nan,
            "air_s": dur - contact_s if has_contact else np.nan,
            "set_down_s": sig.t[a] if c[0] else (sig.t[a + downs[0]] if len(downs) else np.nan),
            "lift_off_s": sig.t[a + ups[-1]] if len(ups) else np.nan,
            "contacts": len(downs) + (1 if c[0] else 0),
            "gyro_peak_dps": float(x[a:b].max()),
        })
    df = pd.DataFrame(rows, columns=["t_s", "period_s", "contact_s", "air_s", "set_down_s", "lift_off_s",
                                     "contacts", "gyro_peak_dps"])
    df["cadence_spm"] = 60.0 / df["period_s"]
    df["push_rate_spm"] = 2 * df["cadence_spm"]
    df["contact_frac"] = df["contact_s"] / df["period_s"]
    return df, method


def gps_frame(sess: Session) -> pd.DataFrame | None:
    g = sess.gps
    if g is None or len(g) < 2:
        return None
    df = pd.DataFrame({c: g[c] for c in g.dtype.names})
    df["t_s"] = (df["elapsed_ns"] - sess.start_elapsed_ns) / 1e9
    df = df.sort_values("t_s").reset_index(drop=True)
    sp = df["speed_mps"].fillna(0).to_numpy()
    tt = df["t_s"].to_numpy()
    seg = np.concatenate([[0], (sp[1:] + sp[:-1]) / 2 * np.diff(tt)])
    df["dist_m"] = np.cumsum(seg)
    return df


def _attach_gps(strokes: pd.DataFrame, gps: pd.DataFrame | None) -> None:
    if gps is None or strokes.empty:
        strokes["speed_mps"] = np.nan
        strokes["dist_km"] = np.nan
        strokes["dist_per_push_m"] = np.nan
        return
    mid = strokes["t_s"] + strokes["period_s"] / 2
    strokes["speed_mps"] = np.interp(mid, gps["t_s"], gps["speed_mps"].interpolate().bfill().ffill())
    strokes["dist_km"] = np.interp(strokes["t_s"], gps["t_s"], gps["dist_m"]) / 1000
    # One cycle of this foot covers two pushes (one per foot).
    strokes["dist_per_push_m"] = strokes["speed_mps"] * strokes["period_s"] / 2


def _drift(strokes: pd.DataFrame, by: str, frac: float) -> dict:
    s = strokes.dropna(subset=[by])
    if len(s) < 20:
        return {}
    lo, hi = s[by].quantile(frac), s[by].quantile(1 - frac)
    first, last = s[s[by] <= lo], s[s[by] >= hi]
    out = {}
    for col in ("push_rate_spm", "dist_per_push_m", "contact_frac", "speed_mps"):
        a, b = first[col].median(), last[col].median()
        if np.isfinite(a) and np.isfinite(b) and a:
            out[col] = {"first": a, "last": b, "change_pct": 100 * (b - a) / a}
    return out


def analyze(sess: Session, tag: TagData | None = None, params: Params | None = None,
            method: str = "auto") -> Result:
    p = params or Params()
    tag = tag or sess.tags[0]
    warnings = []
    if len(tag.raw) == 0:
        raise ValueError("tag log has no IMU samples")
    clock = fit_clocks(tag)
    if abs(clock.rate_hz / tag.odr_hz - 1) > 0.02:
        warnings.append(f"sample rate {clock.rate_hz:.2f} Hz is far from nominal {tag.odr_hz} Hz")
    gaps = tag.gaps()
    if gaps:
        warnings.append(f"{len(gaps)} gap(s), {sum(n for _, n in gaps)} samples missing")
    sig = compute_signals(sess, tag, clock, p)
    requested = method
    strokes, method = detect_strokes(sig, clock.rate_hz, p, method)
    if requested == "auto" and method == "gyro" and len(strokes):
        warnings.append(f"road vibration doesn't match the gyro cadence (separation {sig.vib_separation:.1f}); "
                        "cycles from |gyro|, contact/air split is unreliable")
    gps = gps_frame(sess)
    _attach_gps(strokes, gps)
    if gps is None:
        warnings.append("no GPS: no speed or distance per push")

    moving_s = sig.moving.sum() / clock.rate_hz
    summary = {
        "tag": tag.meta.address,
        "position": tag.meta.position,
        "duration_s": float(sig.t[-1] - sig.t[0]),
        "moving_s": float(moving_s),
        "sample_rate_hz": clock.rate_hz,
        "clock_residual_ms": clock.rx_residual_ms,
        "samples_missing": int(sum(n for _, n in gaps)),
        "strokes": int(len(strokes)),
        "cycle_method": method,
        "cycle_period_s": float(strokes["period_s"].median()) if len(strokes) else None,
        "push_rate_spm": float(strokes["push_rate_spm"].median()) if len(strokes) else None,
        "contact_frac": float(strokes["contact_frac"].median()) if len(strokes) else None,
        "vib_separation": sig.vib_separation,
    }
    per_km = None
    if gps is not None:
        summary["distance_km"] = float(gps["dist_m"].iloc[-1] / 1000)
        moving_gps = gps[gps["speed_mps"] > 1.5]
        summary["avg_moving_speed_kmh"] = float(moving_gps["speed_mps"].mean() * 3.6) if len(moving_gps) else None
        summary["dist_per_push_m"] = float(strokes["dist_per_push_m"].median()) if len(strokes) else None
        summary["drift"] = _drift(strokes, "dist_km", p.fatigue_frac)
        if len(strokes):
            km = np.floor(strokes["dist_km"]).astype("Int64")
            per_km = strokes.groupby(km).agg(
                strokes=("t_s", "size"),
                push_rate_spm=("push_rate_spm", "median"),
                dist_per_push_m=("dist_per_push_m", "median"),
                contact_frac=("contact_frac", "median"),
                speed_kmh=("speed_mps", lambda s: s.median() * 3.6),
            )
            per_km.index.name = "km"
    else:
        summary["drift"] = _drift(strokes, "t_s", p.fatigue_frac)
    return Result(tag, clock, sig, strokes, gps, summary, per_km, warnings)


def format_summary(r: Result) -> str:
    s = r.summary
    f = lambda v, fmt: "-" if v is None or (isinstance(v, float) and not np.isfinite(v)) else fmt.format(v)
    lines = [
        f"tag {s['tag']} ({s['position']})",
        f"  duration        {s['duration_s'] / 60:.1f} min, skating {s['moving_s'] / 60:.1f} min",
        f"  sample rate     {s['sample_rate_hz']:.2f} Hz, clock fit ±{f(s['clock_residual_ms'], '{:.2f}')} ms,"
        f" {s['samples_missing']} samples missing",
        f"  stroke cycles   {s['strokes']} (this foot, by {s['cycle_method']}),"
        f" median period {f(s['cycle_period_s'], '{:.2f}')} s",
        f"  push rate       {f(s['push_rate_spm'], '{:.1f}')} /min (both feet, est. 2x this foot)",
        f"  ground contact  {f(s['contact_frac'], '{:.0%}')} of each cycle"
        f" (vibration separation {s['vib_separation']:.1f})",
    ]
    if "distance_km" in s:
        lines += [
            f"  distance        {s['distance_km']:.2f} km, avg moving speed {f(s['avg_moving_speed_kmh'], '{:.1f}')} km/h",
            f"  distance/push   {f(s['dist_per_push_m'], '{:.2f}')} m",
        ]
    if s.get("drift"):
        basis = "distance" if "distance_km" in s else "time"
        lines.append(f"  fatigue drift (first vs last 20 % by {basis}):")
        names = {"push_rate_spm": "push rate", "dist_per_push_m": "dist/push", "contact_frac": "contact",
                 "speed_mps": "speed"}
        for k, v in s["drift"].items():
            lines.append(f"    {names[k]:<10} {v['first']:8.2f} -> {v['last']:8.2f}  ({v['change_pct']:+.1f} %)")
    for w in r.warnings:
        lines.append(f"  warning: {w}")
    return "\n".join(lines)
