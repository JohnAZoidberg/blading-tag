"""Ride map: route over a basemap, a GPS-altitude profile and climbs.

For rides with only phone GPS and heart rate. Speed, HR, distance and stops
come from `cycling.analyze`. Altitude is the phone's GPS altitude (Android's
`Location.getAltitude`, height above the WGS84 ellipsoid, not sea level). It
is noisy, so it is smoothed along distance before deriving climbing totals and
grade. These are estimates, not barometric or survey elevations.

The page draws its own Web-Mercator tile layer. Opening it requests basemap
tiles for the ride area from the chosen tile provider; without a network the
route, profile and tables still work on a blank background.
"""
from __future__ import annotations

import json
from importlib import resources
from pathlib import Path

import numpy as np

from . import cycling
from .session import Session, load_session

BIN_S = 5.0            # cycling.analyze timeline step
POS_ACC_M = 30.0       # same horizontal-accuracy gate as the cycling route
RUN_GAP_S = 30.0       # an altitude gap longer than this splits the profile
RUN_JUMP_M = 500.0     # ...as does a distance jump without altitude between
SMOOTH_M = 60.0        # ± window along distance for median then mean
GRADE_M = 50.0         # grade = rise over ±50 m of distance
HYSTERESIS_M = 3.0     # climbing/descending totals ignore smaller wiggles
CLIMB_PIVOT_M = 10.0   # a climb may contain dips up to this deep
CLIMB_MIN_GAIN_M = 25.0
TRIM_M = 2.0           # climb ends: first within this of the top, last within it of the bottom
CLIMB_MIN_GRADE = 2.0  # percent
GRADE_BANDS = [(-99, -6), (-6, -3), (-3, -1), (-1, 1), (1, 3), (3, 6), (6, 9), (9, 99)]


def _bin_altitude(sess: Session, n: int) -> np.ndarray:
    """Median GPS altitude of good fixes in each 5 s timeline bin."""
    alt = np.full(n, np.nan)
    g = sess.gps
    if g is None or not len(g):
        return alt
    t = (g['elapsed_ns'] - sess.start_elapsed_ns) / 1e9
    ok = (np.isfinite(g['alt_m']) & np.isfinite(g['lat']) & np.isfinite(g['lon'])
          & np.isfinite(g['h_acc_m']) & (g['h_acc_m'] >= 0) & (g['h_acc_m'] <= POS_ACC_M)
          & (t >= 0) & (np.abs(g['alt_m']) < 9000))
    k = np.floor(t[ok] / BIN_S).astype(int)
    a = g['alt_m'][ok]
    keep = k < n
    k, a = k[keep], a[keep]
    order = np.argsort(k, kind='stable')
    k, a = k[order], a[order]
    for idx, start in zip(*np.unique(k, return_index=True)):
        end = np.searchsorted(k, idx, side='right')
        alt[idx] = np.median(a[start:end])
    return alt


def _runs(t, d, alt):
    """Index arrays of contiguous stretches that have altitude."""
    idx = np.flatnonzero(np.isfinite(alt) & np.isfinite(d))
    if not len(idx):
        return []
    breaks = np.flatnonzero((np.diff(t[idx]) > RUN_GAP_S) | (np.abs(np.diff(d[idx])) > RUN_JUMP_M)) + 1
    return [r for r in np.split(idx, breaks) if len(r) >= 2]


def _window(d, values, half, fn):
    lo = np.searchsorted(d, d - half, side='left')
    hi = np.searchsorted(d, d + half, side='right')
    return np.array([fn(values[a:b]) for a, b in zip(lo, hi)])


def smooth_along_distance(d, alt, half=SMOOTH_M):
    """Median then mean over ±half metres; d must be non-decreasing."""
    return _window(d, _window(d, alt, half, np.median), half, np.mean)


def grade_along_distance(d, alt, half=GRADE_M):
    """Percent grade from rise over ±half metres, clipped to the run."""
    du, inv = np.unique(d, return_inverse=True)
    au = np.bincount(inv, alt) / np.bincount(inv)
    if du[-1] - du[0] < half:
        return np.full(len(d), np.nan)
    lo = np.clip(d - half, du[0], du[-1])
    hi = np.clip(d + half, du[0], du[-1])
    span = hi - lo
    with np.errstate(invalid='ignore', divide='ignore'):
        g = 100 * (np.interp(hi, du, au) - np.interp(lo, du, au)) / span
    g[span < half] = np.nan
    return g


def climb_descent(alt, threshold=HYSTERESIS_M):
    """Total climbing and descending between turning points at least threshold apart."""
    alt = np.asarray(alt, float)
    if len(alt) < 2:
        return 0., 0.
    p = pivots(alt, threshold)
    if not p:
        return 0., 0.
    steps = np.diff(alt[[0, *p, len(alt) - 1]])
    # The legs before the first and after the last turning point are under the threshold if they reverse.
    return float(steps[steps > 0].sum()), float(-steps[steps < 0].sum())


def pivots(alt, threshold=CLIMB_PIVOT_M):
    """Alternating low/high turning points that differ by at least threshold."""
    n = len(alt)
    if n < 2:
        return []
    lo = hi = 0
    trend = 0
    out: list[int] = []
    ext = 0
    for i in range(1, n):
        x = alt[i]
        if trend == 0:
            if x > alt[hi]:
                hi = i
            if x < alt[lo]:
                lo = i
            if alt[hi] - alt[lo] >= threshold:
                trend = 1 if hi > lo else -1
                out = [lo if trend == 1 else hi]
                ext = hi if trend == 1 else lo
        elif trend == 1:
            if x > alt[ext]:
                ext = i
            elif alt[ext] - x >= threshold:
                out.append(ext)
                trend, ext = -1, i
        else:
            if x < alt[ext]:
                ext = i
            elif x - alt[ext] >= threshold:
                out.append(ext)
                trend, ext = 1, i
    if trend:
        out.append(ext)
    return out


def _mean(x):
    x = np.asarray(x, float)
    x = x[np.isfinite(x)]
    return float(x.mean()) if len(x) else None


def analyze(path: str | Path | Session) -> dict:
    sess = path if isinstance(path, Session) else load_session(path)
    base = cycling.analyze(sess)
    tl = base['timeline']
    n = len(tl)
    col = lambda k: np.array([np.nan if r[k] is None else r[k] for r in tl], float)
    t, d = col('t'), col('distance') * 1000
    speed, hr, lat, lon = col('speed'), col('hr'), col('lat'), col('lon')
    raw = _bin_altitude(sess, n)
    raw[~(np.isfinite(lat) & np.isfinite(lon))] = np.nan
    alt = np.full(n, np.nan)
    grade = np.full(n, np.nan)
    up = down = 0.
    climbs = []
    for run in _runs(t, d, raw):
        a = smooth_along_distance(d[run], raw[run])
        alt[run] = a
        grade[run] = grade_along_distance(d[run], a)
        u, dn = climb_descent(a)
        up, down = up + u, down + dn
        p = pivots(a)
        for i, j in zip(p, p[1:]):
            if a[j] <= a[i]:
                continue
            gain = a[j] - a[i]
            # Pivots sit on the last noisy extreme of a plateau; the climb runs from
            # leaving the bottom to first reaching the top (within TRIM_M).
            j = i + int(np.argmax(a[i:j + 1] >= a[j] - TRIM_M))
            i = i + int(np.flatnonzero(a[i:j + 1] <= a[i] + TRIM_M)[-1])
            length = d[run][j] - d[run][i]
            if gain < CLIMB_MIN_GAIN_M or length <= 0 or 100 * gain / length < CLIMB_MIN_GRADE:
                continue
            gi, gj = run[i], run[j]
            sl = slice(gi, gj + 1)
            moving = speed[sl] >= cycling.MOVING_MPS * 3.6
            secs = float(t[gj] - t[gi]) + BIN_S
            climbs.append(dict(start_i=int(gi), end_i=int(gj), start_km=float(d[gi] / 1000), length_km=float(length / 1000),
                               gain_m=float(gain), bottom_m=float(a[i]), top_m=float(a[j]),
                               grade_pct=float(100 * gain / length),
                               max_grade_pct=float(np.nanmax(grade[sl])) if np.isfinite(grade[sl]).any() else None,
                               duration_s=secs, speed_kmh=_mean(speed[sl][moving]), hr_bpm=_mean(hr[sl]),
                               vam_m_h=float(gain / secs * 3600)))
    climbs.sort(key=lambda c: c['start_km'])
    moving = speed >= cycling.MOVING_MPS * 3.6
    bands = []
    for lo, hi in GRADE_BANDS:
        m = moving & (grade >= lo) & (grade < hi)
        if m.sum() * BIN_S < 60:
            continue
        label = f'below {hi:g}' if lo == -99 else f'{lo:g} and steeper' if hi == 99 else f'{lo:g} to {hi:g}'
        bands.append(dict(band=label, lo=lo, hi=hi, time_s=float(m.sum() * BIN_S),
                          speed_kmh=_mean(speed[m]), hr_bpm=_mean(hr[m])))
    have = np.isfinite(alt)
    summary = dict(base['summary'], climb_m=up, descent_m=down,
                   alt_min_m=float(np.nanmin(alt)) if have.any() else None,
                   alt_max_m=float(np.nanmax(alt)) if have.any() else None,
                   alt_coverage=float(have.mean()) if n else 0., climbs=len(climbs))
    findings = list(base['findings'][:3])
    if have.any():
        findings.append(f"GPS altitude runs from {summary['alt_min_m']:.0f} to {summary['alt_max_m']:.0f} m, with an estimated {up:.0f} m of climbing and {down:.0f} m of descending. Altitude covers {100 * summary['alt_coverage']:.0f}% of the ride.")
        if climbs:
            big = max(climbs, key=lambda c: c['gain_m'])
            hr_txt = f", heart rate averaging {big['hr_bpm']:.0f} bpm" if big['hr_bpm'] else ''
            spd = f" at {big['speed_kmh']:.1f} km/h" if big['speed_kmh'] else ''
            findings.append(f"{len(climbs)} climb{'s gain' if len(climbs) != 1 else ' gains'} at least {CLIMB_MIN_GAIN_M:.0f} m. {'The biggest' if len(climbs) > 1 else 'It'} starts at km {big['start_km']:.1f} and gains {big['gain_m']:.0f} m over {big['length_km']:.1f} km ({big['grade_pct']:.1f}% average), taking {big['duration_s'] / 60:.0f} min{spd}{hr_txt}.")
        flat = next((b for b in bands if b['lo'] == -1), None)
        steep = [b for b in bands if b['lo'] >= 3 and b['time_s'] >= 120]
        if flat and steep and flat['speed_kmh'] and steep[0]['speed_kmh']:
            s = steep[0]
            hr_cmp = (f" Heart rate is {s['hr_bpm']:.0f} versus {flat['hr_bpm']:.0f} bpm."
                      if s['hr_bpm'] and flat['hr_bpm'] else '')
            findings.append(f"On flat ground (−1 to +1%) moving speed averages {flat['speed_kmh']:.1f} km/h; on {s['band']}% grades it's {s['speed_kmh']:.1f} km/h.{hr_cmp} Grade is from smoothed GPS altitude, so short ramps are flattened.")
    else:
        findings.append('No usable GPS altitude: the profile, climbs and grade are unavailable.')
    r = lambda x, k: [None if not np.isfinite(v) else round(float(v), k) for v in x]
    points = dict(t=r(t, 0), d=r(d / 1000, 4), lat=r(lat, 6), lon=r(lon, 6), alt=r(alt, 1), raw=r(raw, 1),
                  grade=r(grade, 1), speed=r(speed, 1), hr=r(hr, 0))
    methods = [
        'Speed, heart rate, distance and stops come from the cycling report analysis: a one-second grid, 5-second medians, distance integrated from GPS speed, moving means ≥3.6 km/h.',
        f'Altitude is the phone GPS altitude (height above the WGS84 ellipsoid, which differs from sea level by the local geoid offset, up to tens of metres). Fixes need horizontal accuracy ≤{POS_ACC_M:.0f} m; there is no vertical accuracy, and GPS altitude typically wanders by several metres.',
        f'The profile takes a ±{SMOOTH_M:.0f} m median then mean along distance, and breaks across gaps over {RUN_GAP_S:.0f} s. Climbing and descending totals ignore changes under {HYSTERESIS_M:.0f} m. Grade is the rise over ±{GRADE_M:.0f} m of distance.',
        f'Climbs run from a low to a high turning point (dips under {CLIMB_PIVOT_M:.0f} m don\'t split them) and need ≥{CLIMB_MIN_GAIN_M:.0f} m gain at ≥{CLIMB_MIN_GRADE:.0f}% average. VAM is metres climbed per hour of elapsed time on the climb, stops included.',
        'No power, wind, calories or training zones are inferred. Grade bands compare moving 5-second bins only and describe this ride, not a causal effect of slope.',
        'The basemap is fetched from the selected tile provider (Esri or OpenTopoMap) when the page is opened, which tells that provider the ride area. Choose "None" to draw the route without tiles. Dashed lines bridge GPS gaps longer than 30 s.',
    ]
    return cycling._finite(dict(session=base['session'], start_utc=base['start_utc'], gear=base['gear'],
                                summary=summary, findings=findings, points=points, climbs=climbs,
                                grade_bands=bands, stops=base['stops'], methods=methods))


def write_report(paths, output: str | Path, title: str | None = None) -> list[dict]:
    """Write one page for one or more rides (a selector switches between them)."""
    if isinstance(paths, (str, Path, Session)):
        paths = [paths]
    rides = [analyze(p) for p in paths]
    rides.sort(key=lambda r: r['start_utc'])
    data = dict(title=title or ('Ride map' if len(rides) == 1 else f'{len(rides)} rides'), rides=rides)
    payload = json.dumps(data, allow_nan=False, separators=(',', ':')).replace('<', '\\u003c').replace('>', '\\u003e').replace('&', '\\u0026')
    template = resources.files('bladetag').joinpath('ride_map.html').read_text()
    Path(output).write_text(template.replace('__RIDE_DATA__', payload))
    return rides
