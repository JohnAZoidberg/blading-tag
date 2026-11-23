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
from datetime import datetime
from importlib import resources
from pathlib import Path

import numpy as np

from . import cycling
from .road_fill import PORTAL_FIXES, fill_gaps
from .session import Session, load_session

BIN_S = 5.0            # cycling.analyze timeline step
POS_ACC_M = 30.0       # same horizontal-accuracy gate as the cycling route
RUN_GAP_S = 30.0       # an altitude gap longer than this splits the profile
RUN_JUMP_M = 500.0     # ...as does a distance jump without altitude between
SMOOTH_M = 60.0        # ± window along distance for median then mean
GRADE_M = 100.0        # grade = rise over ±100 m of distance
HYSTERESIS_M = 3.0     # climbing/descending totals ignore smaller wiggles
CLIMB_PIVOT_M = 10.0   # a climb may contain dips up to this deep
CLIMB_MIN_GAIN_M = 25.0
TRIM_M = 2.0           # climb ends: first within this of the top, last within it of the bottom
CLIMB_MIN_GRADE = 2.0  # percent
REST_MIN_S = 300.0     # rest stops: at least this long, from first to last stopped second
REST_JOIN_S = 90.0     # stops this close together, with little movement between, are one rest
REST_JOIN_M = 150.0
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


def rest_stops(stops, t, d, hr, alt) -> list[dict]:
    """Rests of at least REST_MIN_S, merging stops split by a short shuffle (walking the bike, rolling a few metres)."""
    if not len(t):
        return []
    at = lambda s: int(min(len(t) - 1, max(0, np.searchsorted(t, s, side='right') - 1)))
    merged = []
    for s in sorted(stops, key=lambda s: s['start_s']):
        if merged:
            m = merged[-1]
            if s['start_s'] - m['end_s'] <= REST_JOIN_S and d[at(s['start_s'])] - d[at(m['end_s'])] <= REST_JOIN_M:
                m['end_s'], m['stopped_s'] = s['end_s'], m['stopped_s'] + s['duration_s']
                continue
        merged.append(dict(start_s=s['start_s'], end_s=s['end_s'], stopped_s=s['duration_s']))
    rests, prev_i = [], 0
    end = t[-1] + BIN_S
    for m in merged:
        # Waiting before the start or after the finish isn't a rest.
        if m['end_s'] - m['start_s'] < REST_MIN_S or m['start_s'] <= 10 or m['end_s'] >= end - 10:
            continue
        i0, i1 = at(m['start_s']), at(m['end_s'] - 1e-6)
        sl = slice(i0, i1 + 1)
        before = hr[max(0, i0 - 6):i0 + 1]
        rests.append(dict(start_i=i0, end_i=i1, mid_i=(i0 + i1) // 2, start_s=m['start_s'], end_s=m['end_s'],
                          duration_s=m['end_s'] - m['start_s'], stopped_s=m['stopped_s'], km=float(d[i0] / 1000),
                          alt_m=_mean(alt[sl]), hr_bpm=_mean(hr[sl]),
                          hr_min_bpm=float(np.nanmin(hr[sl])) if np.isfinite(hr[sl]).any() else None,
                          hr_arrive_bpm=_mean(before),
                          since_km=float((d[i0] - d[prev_i]) / 1000), since_s=float(t[i0] - t[prev_i])))
        prev_i = i1
    return rests


def _rest_finding(rests):
    if not rests:
        return []
    total = sum(r['duration_s'] for r in rests)
    longest = max(rests, key=lambda r: r['duration_s'])
    return [f"{len(rests)} rest stop{'s' if len(rests) != 1 else ''} of at least {REST_MIN_S / 60:.0f} minutes add up to {total / 60:.0f} min. "
            f"The longest is {longest['duration_s'] / 60:.0f} min at km {longest['km']:.1f}."]


def _mean(x):
    x = np.asarray(x, float)
    x = x[np.isfinite(x)]
    return float(x.mean()) if len(x) else None


METHODS = [
    'Speed, heart rate, distance and stops come from the cycling report analysis: a one-second grid, 5-second medians, distance integrated from GPS speed, moving means ≥3.6 km/h.',
    f'Altitude is the phone GPS altitude (height above the WGS84 ellipsoid, which differs from sea level by the local geoid offset, up to tens of metres). Fixes need horizontal accuracy ≤{POS_ACC_M:.0f} m; there is no vertical accuracy, and GPS altitude typically wanders by several metres.',
    f'The profile takes a ±{SMOOTH_M:.0f} m median then mean along distance, and breaks across gaps over {RUN_GAP_S:.0f} s. Climbing and descending totals add the legs between turning points at least {HYSTERESIS_M:.0f} m apart. Grade is the rise over ±{GRADE_M:.0f} m of distance.',
    f'Climbs run from a low to a high turning point (dips under {CLIMB_PIVOT_M:.0f} m don\'t split them) and need ≥{CLIMB_MIN_GAIN_M:.0f} m gain at ≥{CLIMB_MIN_GRADE:.0f}% average. VAM is metres climbed per hour of elapsed time on the climb, stops included.',
    'No power, wind, calories or training zones are inferred. Grade bands compare moving 5-second bins only and describe this ride, not a causal effect of slope.',
    'The basemap is fetched from the selected tile provider (Esri or OpenTopoMap) when the page is opened, which tells that provider the ride area. Choose "None" to draw the route without tiles. Dashed lines bridge GPS gaps longer than 30 s.',
    'GPS gaps (tunnels) between stable stretches of fixes with horizontal accuracy ≤15 m are filled along OpenStreetMap roads: the shortest road path between the gap ends, preferring roads open to bicycles unless the implied speed fits the other route better. The time is spread evenly along it at constant speed. Only boxes around the gaps are sent to the Overpass API; answers are cached. Without a road path the distance is the speed around the gap times its duration, on a straight line. Map data © OpenStreetMap contributors (ODbL).',
    'Altitude in a gap, and for 10 s beyond each end where GPS altitude near a tunnel portal is unreliable, is linear in distance between the median altitudes 10–20 fixes outside the gap. Gaps under 50 m are stops: the position is held.',
    f'Rest stops join stops (GPS speed under 3.6 km/h for 10 s or more) that are under {REST_JOIN_S:.0f} s and {REST_JOIN_M:.0f} m apart, and keep those lasting {REST_MIN_S / 60:.0f} minutes or more from first to last stopped second, except waiting at the start or the finish of a recording. "Since previous" counts from the ride start or the end of the previous rest. Arrival HR is the mean over the 30 s before the stop.',
    'A whole day combines its rides end to end: distance continues from one ride to the next, the profile breaks between rides, and time between rides counts as neither elapsed nor moving time.',
]


def _terrain(t, d, speed, hr, lat, lon, raw, fill):
    """Smoothed altitude, grade, climbing totals, climbs and grade bands on a timeline."""
    n = len(t)
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
    summary = dict(climb_m=up, descent_m=down,
                   alt_min_m=float(np.nanmin(alt)) if have.any() else None,
                   alt_max_m=float(np.nanmax(alt)) if have.any() else None,
                   alt_coverage=float(have.mean()) if n else 0., climbs=len(climbs))
    findings = []
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
                  grade=r(grade, 1), speed=r(speed, 1), hr=r(hr, 0), fill=[int(x) for x in fill])
    return summary, findings, points, climbs, bands


def _measured_coverage(sess: Session, duration: float) -> float:
    """Share of elapsed seconds with a recent real GPS speed (as cycling.analyze counts it)."""
    g = sess.gps
    if g is None or not len(g) or not duration:
        return 0.
    ok = (np.isfinite(g['speed_mps']) & (g['speed_mps'] >= 0) & (g['speed_mps'] <= 40)
          & np.isfinite(g['h_acc_m']) & (g['h_acc_m'] >= 0) & (g['h_acc_m'] <= 20))
    ft = np.sort((g['elapsed_ns'][ok] - sess.start_elapsed_ns) / 1e9)
    if not len(ft):
        return 0.
    t = np.arange(0., duration, 1.)
    ix = np.searchsorted(ft, t, side='right') - 1
    held = (ix >= 0) & (t - ft[np.maximum(ix, 0)] < cycling.GPS_HOLD_S)
    return float(held.sum() / len(t))


def _gap_altitude(t, d, raw, gaps):
    """Drop altitude near tunnel portals and interpolate it across each gap."""
    fill = np.zeros(len(t), int)
    for g in gaps:
        inside = (t + BIN_S > g['t0_s']) & (t < g['t1_s'])
        if g['method'] in ('road', 'speed'):
            fill[inside] = 1 if g['method'] == 'road' else 2
        margin = PORTAL_FIXES + BIN_S
        ia = np.searchsorted(t, g['t0_s'] - margin, side='right') - 1
        ib = np.searchsorted(t, g['t1_s'] + margin, side='left')
        if ia < 0 or ib >= len(t):
            raw[max(ia, 0):min(ib, len(t))] = np.nan
            continue
        before, after = g.get('portal_alt_m') or (None, None)
        a0 = before if before is not None else raw[ia]
        a1 = after if after is not None else raw[ib]
        if not (np.isfinite(a0) and np.isfinite(a1)) or d[ib] < d[ia]:
            raw[ia + 1:ib] = np.nan
            continue
        raw[ia], raw[ib] = a0, a1
        span = d[ib] - d[ia]
        raw[ia + 1:ib] = a0 + (a1 - a0) * ((d[ia + 1:ib] - d[ia]) / span if span > 0 else 0.)
    return fill


def analyze(path: str | Path | Session, roads=None) -> dict:
    """``roads``: None fetches OpenStreetMap roads for GPS gaps, False skips that,
    or a callable returning ways (see road_fill.fill_gaps)."""
    sess = path if isinstance(path, Session) else load_session(path)
    filled, gaps = fill_gaps(sess, roads)
    base = cycling.analyze(filled)
    tl = base['timeline']
    col = lambda k: np.array([np.nan if r[k] is None else r[k] for r in tl], float)
    t, d = col('t'), col('distance') * 1000
    speed, hr, lat, lon = col('speed'), col('hr'), col('lat'), col('lon')
    raw = _bin_altitude(sess, len(tl))         # real fixes only
    raw[~(np.isfinite(lat) & np.isfinite(lon))] = np.nan
    fill = _gap_altitude(t, d, raw, gaps)
    terrain, findings, points, climbs, bands = _terrain(t, d, speed, hr, lat, lon, raw, fill)
    alt = np.array([np.nan if v is None else v for v in points['alt']], float)
    rests = rest_stops(base['stops'], t, d, hr, alt)
    summary = dict(base['summary'], **terrain, rests=len(rests), rest_s=sum(r['duration_s'] for r in rests))
    journeys = [g for g in gaps if g['method'] in ('road', 'speed')]
    summary.update(gps_coverage=_measured_coverage(sess, summary['duration_s']),
                   filled_km=sum(g['length_m'] for g in journeys) / 1000,
                   road_km=sum(g['length_m'] for g in journeys if g['method'] == 'road') / 1000,
                   tunnel_km=sum(g['length_m'] * (g['tunnel_share'] or 0) for g in journeys) / 1000,
                   gaps=len(journeys))
    head = base['findings'][:3]
    if journeys:
        S = summary
        guessed = S['filled_km'] - S['road_km']
        head[0] = (f"Distance is {S['distance_km']:.2f} km. {S['distance_km'] - S['filled_km']:.2f} km comes from phone GPS speed and {S['filled_km']:.2f} km crosses {len(journeys)} GPS gaps"
                   + (f", {S['road_km']:.2f} km of it along OpenStreetMap roads ({S['tunnel_km']:.1f} km in tunnels)" if S['road_km'] else '')
                   + (f"; {guessed:.2f} km is estimated from the speed around a gap" if guessed > .005 else '')
                   + f". {S['moving_s'] / 60:.1f} minutes at or above 3.6 km/h. Real GPS covers {100 * S['gps_coverage']:.1f}% of elapsed time.")
    rides_gaps = [{k: v for k, v in g.items() if k != 'portal_alt_m'} for g in gaps]
    return cycling._finite(dict(session=base['session'], start_utc=base['start_utc'], gear=base['gear'],
                                summary=summary, findings=head + findings + _rest_finding(rests),
                                points=points, climbs=climbs, rests=rests, grade_bands=bands, stops=base['stops'],
                                gaps=rides_gaps, methods=METHODS))


def _utc(ride):
    return datetime.fromisoformat(ride['start_utc'].replace('Z', '+00:00'))


def combine(rides: list[dict], day: str) -> dict:
    """Join analysed rides end to end into one 'whole day' ride.

    Time stays on the first ride's clock (so clock times stay right) and the
    gap between rides breaks the profile and the route. Distance continues.
    """
    rides = sorted(rides, key=_utc)
    t0 = _utc(rides[0])
    cols = {k: [] for k in ('t', 'd', 'speed', 'hr', 'lat', 'lon', 'raw', 'fill')}
    gaps = []
    stops, starts, rests, d_off, n = [], [], [], 0., 0
    for ride in rides:
        off = (_utc(ride) - t0).total_seconds()
        p = ride['points']
        rests += [dict(r, start_i=r['start_i'] + n, end_i=r['end_i'] + n, mid_i=r['mid_i'] + n, start_s=r['start_s'] + off,
                       end_s=r['end_s'] + off, km=r['km'] + d_off / 1000) for r in ride.get('rests', [])]
        starts.append(n)
        n += len(p['t'])
        arr = lambda k: np.array([np.nan if v is None else v for v in p[k]], float)
        cols['t'].append(arr('t') + off)
        cols['d'].append(arr('d') * 1000 + d_off)
        for k in ('speed', 'hr', 'lat', 'lon', 'raw'):
            cols[k].append(arr(k))
        cols['fill'].append(np.array(p.get('fill') or [0] * len(p['t']), int))
        gaps += [dict(g, t0_s=g['t0_s'] + off, t1_s=g['t1_s'] + off) for g in ride.get('gaps', [])]
        stops += [dict(s, start_s=s['start_s'] + off, end_s=s['end_s'] + off) for s in ride['stops']]
        d_off += ride['summary']['distance_km'] * 1000
    c = {k: np.concatenate(v) for k, v in cols.items()}
    terrain, t_findings, points, climbs, bands = _terrain(c['t'], c['d'], c['speed'], c['hr'], c['lat'], c['lon'], c['raw'], c['fill'])
    S = [r['summary'] for r in rides]
    total = lambda k: float(sum(s[k] or 0 for s in S))
    def weighted(k, w):
        pairs = [(s[k], w(s)) for s in S if s[k] is not None and w(s)]
        return sum(v * x for v, x in pairs) / sum(x for _, x in pairs) if pairs else None
    peak = lambda k: max((s[k] for s in S if s[k] is not None), default=None)
    duration = total('duration_s')
    last = rides[-1]
    summary = dict(duration_s=duration, span_s=(_utc(last) - t0).total_seconds() + last['summary']['duration_s'],
                   distance_km=total('distance_km'), moving_s=total('moving_s'), stopped_s=total('stopped_s'),
                   moving_speed_kmh=weighted('moving_speed_kmh', lambda s: s['moving_s']),
                   peak_speed_kmh=peak('peak_speed_kmh'), max_hr_bpm=peak('max_hr_bpm'),
                   mean_hr_bpm=weighted('mean_hr_bpm', lambda s: s['hr_coverage'] * s['duration_s']),
                   hr_coverage=sum(s['hr_coverage'] * s['duration_s'] for s in S) / duration if duration else 0,
                   gps_coverage=sum(s['gps_coverage'] * s['duration_s'] for s in S) / duration if duration else 0,
                   rides=len(rides), **terrain, rests=len(rests), rest_s=sum(r['duration_s'] for r in rests),
                   **{k: total(k) for k in ('filled_km', 'road_km', 'tunnel_km', 'gaps') if all(k in s for s in S)})
    between = sum(((_utc(b) - _utc(a)).total_seconds() - a['summary']['duration_s']) for a, b in zip(rides, rides[1:]))
    findings = [f"{len(rides)} rides cover {summary['distance_km']:.1f} km" + (f" ({summary['filled_km']:.1f} km of it across GPS gaps, {summary['tunnel_km']:.1f} km in tunnels)" if summary.get('filled_km') else '') + f" with {summary['moving_s'] / 3600:.1f} h moving out of {duration / 3600:.1f} h recorded, plus {between / 3600:.1f} h between rides."]
    if summary['mean_hr_bpm'] is not None:
        findings.append(f"Heart rate averages {summary['mean_hr_bpm']:.0f} bpm across the day and reaches {summary['max_hr_bpm']:.0f} bpm; valid contact covers {100 * summary['hr_coverage']:.1f}% of recorded time.")
    return cycling._finite(dict(session=' + '.join(r['session'] for r in rides), start_utc=rides[0]['start_utc'],
                                gear=' / '.join(dict.fromkeys(r['gear'] for r in rides if r['gear'])),
                                combined=True, day=day, members=[r['session'] for r in rides], ride_starts=starts, summary=summary,
                                findings=findings + t_findings + _rest_finding(rests), rests=rests,
                                points=points, climbs=climbs, grade_bands=bands, stops=stops, gaps=gaps, methods=METHODS))


def by_day(rides: list[dict]) -> dict[str, list[dict]]:
    """Group rides by the local calendar day they start on (this machine's time zone)."""
    days: dict[str, list[dict]] = {}
    for ride in rides:
        days.setdefault(_utc(ride).astimezone().date().isoformat(), []).append(ride)
    return days


def write_report(paths, output: str | Path, title: str | None = None, roads=None) -> list[dict]:
    """Write one page for one or more rides (a selector switches between them).

    Returns the single rides; the page also holds one combined ride per day with several.
    """
    if isinstance(paths, (str, Path, Session)):
        paths = [paths]
    rides = sorted((analyze(p, roads) for p in paths), key=_utc)
    # Each day with several rides also gets a combined entry, listed before its rides.
    days = [combine(group, day) for day, group in by_day(rides).items() if len(group) > 1]
    data = dict(title=title or ('Ride map' if len(rides) == 1 else f'{len(rides)} rides'), rides=days + rides)
    payload = json.dumps(data, allow_nan=False, separators=(',', ':')).replace('<', '\\u003c').replace('>', '\\u003e').replace('&', '\\u0026')
    template = resources.files('bladetag').joinpath('ride_map.html').read_text()
    Path(output).write_text(template.replace('__RIDE_DATA__', payload))
    return rides
