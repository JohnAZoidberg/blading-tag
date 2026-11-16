"""Gap-aware GPS and heart-rate context for walking, independent of foot IMU.

Totals integrate exact held intervals; plots sample the same signals each second.
A low-speed interval can be a pause or slow movement, not a proven stop.
"""
from __future__ import annotations

import numpy as np

GPS_HOLD_S = 5.0
HR_HOLD_S = 5.0
MOVING_MPS = 0.5


def _number(value):
    return float(value) if np.isfinite(value) else None


def analyze_session(sess):
    origin = sess.start_elapsed_ns
    gps = sess.gps
    gt = np.array([]) if gps is None else (gps['elapsed_ns'] - origin) / 1e9
    hr = sorted(sess.heart_rate, key=lambda row: row['elapsed_ns'])
    ht = np.array([(row['elapsed_ns'] - origin) / 1e9 for row in hr])
    inferred = max([0., *gt[np.isfinite(gt)], *ht[np.isfinite(ht)]])
    for tag in sess.tags:
        if not tag.meta.simulated and len(tag.pkt_rx_ns):
            inferred = max(inferred, float((tag.pkt_rx_ns[-1] - origin) / 1e9))
    end = sess.meta.get('end_elapsed_ns')
    duration = max(0., (int(end) - origin) / 1e9) if end is not None else inferred
    if len(gt):
        keep = np.isfinite(gt) & (gt >= 0) & (gt < duration)
        gps, gt = gps[keep], gt[keep]
        order = np.argsort(gt, kind='stable')
        gps, gt = gps[order], gt[order]
    keep = np.isfinite(ht) & (ht >= 0) & (ht < duration)
    hr, ht = [row for row, good in zip(hr, keep) if good], ht[keep]

    def held(t):
        speed, bpm, lat, lon = [np.full(len(t), np.nan) for _ in range(4)]
        if len(gt):
            ix = np.searchsorted(gt, t, side='right') - 1
            j = np.maximum(ix, 0)
            valid = (ix >= 0) & (t - gt[j] < GPS_HOLD_S)
            accuracy = gps['h_acc_m'][j]
            position = valid & np.isfinite(accuracy) & (accuracy >= 0) & (accuracy <= 20)
            sv = gps['speed_mps'][j]
            sa = gps['speed_acc_mps'][j]
            # Missing speed accuracy is retained, but reported in quality metadata.
            good = position & np.isfinite(sv) & (sv >= 0) & (sv <= 5) & (~np.isfinite(sa) | ((sa >= 0) & (sa <= 1.5)))
            speed[good] = sv[good]
            pos = position & np.isfinite(gps['lat'][j]) & np.isfinite(gps['lon'][j]) & (np.abs(gps['lat'][j]) <= 90) & (np.abs(gps['lon'][j]) <= 180)
            lat[pos], lon[pos] = gps['lat'][j][pos], gps['lon'][j][pos]
        if len(ht):
            ix = np.searchsorted(ht, t, side='right') - 1
            j = np.maximum(ix, 0)
            values = np.array([r.get('bpm') or np.nan for r in hr])
            contact = np.array([r.get('contact_detected') is not False for r in hr])
            good = (ix >= 0) & (t - ht[j] < HR_HOLD_S) & contact[j] & np.isfinite(values[j]) & (values[j] >= 25) & (values[j] <= 240)
            bpm[good] = values[j][good]
        return speed, bpm, lat, lon

    edges = np.unique(np.clip(np.r_[0., duration, gt, gt + GPS_HOLD_S, ht, ht + HR_HOLD_S], 0, duration))
    t, dt = edges[:-1], np.diff(edges)
    speed, bpm, _, _ = held(t)
    known, heart = np.isfinite(speed), np.isfinite(bpm)
    moving = known & (speed >= MOVING_MPS)
    def mean(values, mask):
        return float(np.average(values[mask], weights=dt[mask])) if mask.any() else None
    gps_s, hr_s = float(dt[known].sum()), float(dt[heart].sum())
    distance = float((speed[known] * dt[known]).sum())
    moving_s = float(dt[moving].sum())
    summary = dict(duration_s=duration, distance_km=distance / 1000,
                   gps_coverage=gps_s / duration if duration else 0.,
                   gps_unknown_s=duration - gps_s, moving_s=moving_s,
                   stopped_s=float(dt[known & ~moving].sum()),
                   moving_speed_kmh=mean(speed * 3.6, moving),
                   mean_hr_bpm=mean(bpm, heart), max_hr_bpm=float(bpm[heart].max()) if heart.any() else None,
                   moving_hr_bpm=mean(bpm, heart & moving),
                   hr_coverage=hr_s / duration if duration else 0.)
    stopped = known & ~moving
    transitions = np.diff(np.r_[False, stopped, False].astype(int))
    stops = [dict(start_s=float(t[a]), end_s=float(edges[b]), duration_s=float(edges[b] - t[a]))
             for a, b in zip(np.flatnonzero(transitions == 1), np.flatnonzero(transitions == -1))
             if edges[b] - t[a] >= 10]
    # Compare early/late at the same measured GPS speed, not all early/late points.
    comparisons = []
    for lo in (2., 3., 4., 5., 6.):
        pace = moving & heart & (speed * 3.6 >= lo) & (speed * 3.6 < lo + 1)
        early, late = pace & (t < duration / 2), pace & (t >= duration / 2)
        es, ls = float(dt[early].sum()), float(dt[late].sum())
        if min(es, ls) >= 60:
            comparisons.append(dict(speed_lo_kmh=lo, speed_hi_kmh=lo+1,
                                    early_s=es, late_s=ls, early_hr_bpm=mean(bpm, early), late_hr_bpm=mean(bpm, late),
                                    change_bpm=mean(bpm, late)-mean(bpm, early)))
    timeline_t = np.arange(0., duration, 1.)
    tspeed, thr, lat, lon = held(timeline_t)
    timeline = [dict(t_s=float(ti), speed_kmh=_number(s * 3.6), hr_bpm=_number(h), lat=_number(la), lon=_number(lo))
                for ti, s, h, la, lo in zip(timeline_t, tspeed, thr, lat, lon)]
    findings = []
    if gps_s:
        findings.append(f'Valid phone speed accounts for {distance/1000:.2f} km over {gps_s/60:.1f} minutes ({100*summary["gps_coverage"]:.1f}% of elapsed time); distance excludes missing or rejected GPS intervals.')
        findings.append(f'{moving_s/60:.1f} minutes at or above 1.8 km/h; {summary["stopped_s"]/60:.1f} minutes below it. Low speed includes pauses and slow walking.')
    else:
        findings.append('No usable GPS speed; distance and pace are unavailable.')
    if hr_s:
        findings.append(f'Heart rate averages {summary["mean_hr_bpm"]:.0f} bpm and reaches {summary["max_hr_bpm"]:.0f} bpm, with {100*summary["hr_coverage"]:.1f}% time coverage.')
    else:
        findings.append('No usable heart-rate data with sensor contact.')
    for c in comparisons:
        findings.append(f'At {c["speed_lo_kmh"]:.0f}–{c["speed_hi_kmh"]:.0f} km/h, late-half heart rate is {c["change_bpm"]:+.1f} bpm versus the early half ({c["early_s"]/60:.1f}/{c["late_s"]/60:.1f} minutes). Route, stops, heat and sensor effects remain uncontrolled; this does not establish fatigue.')
    quality = dict(duration_inferred=end is None, gps_fixes=len(gt), hr_notifications=len(ht),
                   gps_speed_accuracy_missing=int(np.count_nonzero(~np.isfinite(gps['speed_acc_mps']))) if gps is not None else 0,
                   gps_hold_s=GPS_HOLD_S, hr_hold_s=HR_HOLD_S,
                   method='Exact interval integration; GPS held ≤5 s, position accuracy ≤20 m, speed ≤18 km/h, speed accuracy ≤1.5 m/s when supplied; HR held ≤5 s with valid contact and 25–240 bpm. No elevation or calorie estimate.')
    return dict(summary=summary, timeline=timeline, stops=stops, pace_hr_comparison=comparisons, findings=findings, quality=quality)
