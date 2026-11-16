"""Conservative phone-GPS/HR cycling summary and an offline interactive report.

Intervals are held forward, never across long gaps. Distance integrates phone
speed, rather than accumulating stationary position jitter. No power, training
zones, grade or cadence is inferred from GPS and heart rate.
"""
from __future__ import annotations

import json
from importlib import resources
from pathlib import Path

import numpy as np

from .session import Session, load_session
from .cycling_signals import cadence_track

GPS_HOLD_S = 10.0
HR_HOLD_S = 5.0
MOVING_MPS = 1.0


def _finite(value):
    if isinstance(value, dict):
        return {k: _finite(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_finite(v) for v in value]
    if isinstance(value, (float, np.floating)):
        return float(value) if np.isfinite(value) else None
    if isinstance(value, np.integer):
        return int(value)
    return value


def analyze(path: str | Path | Session) -> dict:
    """Return JSON-ready cycling measures on the phone's elapsed-time clock."""
    sess = path if isinstance(path, Session) else load_session(path)
    origin = sess.start_elapsed_ns
    gps = sess.gps
    gt = np.array([]) if gps is None else (gps['elapsed_ns'] - origin) / 1e9
    rows = sorted(sess.heart_rate, key=lambda r: r['elapsed_ns'])
    ht = np.array([(r['elapsed_ns'] - origin) / 1e9 for r in rows])
    end = sess.meta.get('end_elapsed_ns')
    duration = max(0., (int(end) - origin) / 1e9) if end else max([0., *gt, *ht])
    # A one-second grid makes all totals explicitly time weighted and leaves gaps blank.
    t = np.arange(0., duration, 1.)
    dt = np.minimum(1., duration - t)
    n = len(t)
    speed, lat, lon, acc, hr = [np.full(n, np.nan) for _ in range(5)]
    if len(gt):
        order = np.argsort(gt, kind='stable')
        gt, gps = gt[order], gps[order]
        ix = np.searchsorted(gt, t, side='right') - 1
        safe = np.maximum(ix, 0)
        valid = (ix >= 0) & (t - gt[safe] < GPS_HOLD_S)
        # Position quality and speed accuracy are separate Android measurements.
        sv = gps['speed_mps'][safe]
        sa = gps['speed_acc_mps'][safe]
        good_speed = valid & np.isfinite(sv) & (sv >= 0) & (sv <= 40) & np.isfinite(gps['h_acc_m'][safe]) & (gps['h_acc_m'][safe] >= 0) & (gps['h_acc_m'][safe] <= 20)
        speed[good_speed] = sv[good_speed]
        good_pos = valid & np.isfinite(gps['lat'][safe]) & np.isfinite(gps['lon'][safe]) & (np.abs(gps['lat'][safe]) <= 90) & (np.abs(gps['lon'][safe]) <= 180) & np.isfinite(gps['h_acc_m'][safe]) & (gps['h_acc_m'][safe] >= 0) & (gps['h_acc_m'][safe] <= 30)
        for target, field in ((lat, 'lat'), (lon, 'lon'), (acc, 'h_acc_m')):
            target[good_pos] = gps[field][safe][good_pos]
    if len(ht):
        ix = np.searchsorted(ht, t, side='right') - 1
        safe = np.maximum(ix, 0)
        hv = np.array([r.get('bpm') or np.nan for r in rows])
        contact = np.array([r.get('contact_detected') is not False for r in rows])
        valid = (ix >= 0) & (t - ht[safe] < HR_HOLD_S) & contact[safe] & (hv[safe] >= 25) & (hv[safe] <= 240)
        hr[valid] = hv[safe][valid]
    moving = speed >= MOVING_MPS
    known = np.isfinite(speed)
    distance = np.cumsum(np.where(known, speed * dt, 0.))
    hr_ok = np.isfinite(hr)
    mean = lambda a, mask: float(np.average(a[mask], weights=dt[mask])) if mask.any() else None
    moving_s = float(dt[moving].sum())
    gps_s = float(dt[known].sum())
    distance_m = float(distance[-1]) if n else 0.
    summary = dict(duration_s=duration, distance_km=distance_m / 1000,
                   moving_s=moving_s, stopped_s=float(dt[known & ~moving].sum()),
                   gps_unknown_s=duration-gps_s, gps_coverage=gps_s/duration if duration else 0,
                   moving_speed_kmh=mean(speed * 3.6, moving),
                   peak_speed_kmh=float(np.nanmax(speed)*3.6) if known.any() else None,
                   mean_hr_bpm=mean(hr, hr_ok), max_hr_bpm=float(np.nanmax(hr)) if hr_ok.any() else None,
                   hr_coverage=float(dt[hr_ok].sum()/duration) if duration else 0,
                   moving_hr_bpm=mean(hr, hr_ok & moving))
    # Contiguous known low-speed intervals. Unknown GPS breaks rather than extends a stop.
    stopped = known & ~moving
    transitions = np.diff(np.r_[False, stopped, False].astype(int))
    stops = []
    for a, b in zip(np.flatnonzero(transitions == 1), np.flatnonzero(transitions == -1)):
        seconds = float(dt[a:b].sum())
        if seconds >= 10:
            stops.append(dict(start_s=float(t[a]), end_s=float(t[b-1]+dt[b-1]), duration_s=seconds))
    splits = []
    if distance_m:
        bins = np.floor(np.r_[0., distance[:-1]]/1000).astype(int)
        for k in np.unique(bins):
            mask = bins == k
            hmask = mask & hr_ok
            elapsed = float(dt[mask].sum())
            km = float(np.where(known & mask, speed*dt, 0.).sum()/1000)
            splits.append(dict(km=int(k+1), distance_km=km, start_s=float(t[mask][0]),
                               elapsed_s=elapsed, moving_s=float(dt[mask & moving].sum()),
                               speed_kmh=km*3600/elapsed if elapsed else None,
                               hr_bpm=mean(hr, hmask), hr_coverage=float(dt[hmask].sum()/elapsed)))
    findings = []
    if gps_s:
        findings.append(f"Recorded {distance_m/1000:.2f} km from valid phone speed, with {moving_s/60:.1f} minutes at or above 3.6 km/h. GPS speed covers {100*summary['gps_coverage']:.1f}% of elapsed time.")
    else:
        findings.append('No usable GPS speed: distance, moving time and route comparisons are unavailable.')
    if hr_ok.any():
        findings.append(f"Heart rate averages {summary['mean_hr_bpm']:.0f} bpm and reaches {summary['max_hr_bpm']:.0f} bpm; valid contact and rate cover {100*summary['hr_coverage']:.1f}% of the recording.")
    else:
        findings.append('No usable heart-rate data with sensor contact.')
    if stops:
        longest = max(stops, key=lambda s: s['duration_s'])
        findings.append(f"{len(stops)} low-speed periods last at least 10 seconds. The longest is {longest['duration_s']/60:.1f} minutes, starting {longest['start_s']/60:.1f} minutes in. These include waiting, walking and slow riding.")
    # Compare moving portions on either side of the main break, excluding startup/end.
    main_break = max(stops, key=lambda row: row['duration_s']) if stops else None
    if main_break and main_break['duration_s'] >= 120:
        early = moving & hr_ok & (t >= duration*.05) & (t < main_break['start_s'])
        late = moving & hr_ok & (t >= main_break['end_s']) & (t < duration*.95)
        comparison_label = 'before the longest stop versus after it'
    else:
        early = moving & hr_ok & (t >= duration*.1) & (t < duration*.4)
        late = moving & hr_ok & (t >= duration*.6) & (t < duration*.9)
        comparison_label = '10–40% versus 60–90% of elapsed time'
    comparison = []
    for lo in range(10, 40, 5):
        a, b = early & (speed*3.6 >= lo) & (speed*3.6 < lo+5), late & (speed*3.6 >= lo) & (speed*3.6 < lo+5)
        if dt[a].sum() >= 60 and dt[b].sum() >= 60:
            comparison.append(dict(speed_band=f'{lo}–{lo+5}', early_hr=mean(hr, a), late_hr=mean(hr, b), early_s=float(dt[a].sum()), late_s=float(dt[b].sum())))
    if comparison:
        best = max(comparison, key=lambda r: min(r['early_s'], r['late_s']))
        findings.append(f"At {best['speed_band']} km/h, moving heart rate averages {best['early_hr']:.0f} bpm versus {best['late_hr']:.0f} later ({comparison_label}). This is a speed-matched description, not a fatigue test: wind, slope, temperature and HR lag can all change it.")
    cadence = cadence_track(sess)
    accepted = [r for r in cadence if r['rpm'] is not None]
    summary['speed_accuracy_median_mps'] = float(np.nanmedian(gps['speed_acc_mps'])) if gps is not None and np.isfinite(gps['speed_acc_mps']).any() else None
    summary['cadence_rpm'] = float(np.median([r['rpm'] for r in accepted])) if accepted else None
    summary['cadence_windows'] = len(accepted)
    development = [r['speed_kmh']/3.6*60/r['rpm'] for r in accepted]
    summary['development_m'] = float(np.median(development)) if development else None
    if accepted:
        findings.append(f"The foot IMU estimates a median {summary['cadence_rpm']:.1f} crank rpm in {len(accepted)} accepted eight-second windows (sampled every two seconds). Estimated distance per crank turn is {summary['development_m']:.2f} m. These are experimental pedal estimates without a crank-sensor reference; rejected windows are unclassified, not coasting.")
        speeds = np.array([r['speed_kmh'] for r in accepted])
        rpms = np.array([r['rpm'] for r in accepted])
        if len(accepted) >= 30 and np.std(speeds) > 0 and np.std(rpms) > 0:
            correlation = float(np.corrcoef(speeds, rpms)[0, 1])
            summary['speed_cadence_r'] = correlation
            findings.append(f"In the accepted windows, speed and estimated cadence have Pearson r = {correlation:+.2f}. A weak relationship is consistent with changing gears to hold cadence, but GPS, hills and missed pedal windows also affect this comparison.")
    # 5-second medians keep the page compact while retaining blank gaps.
    timeline = []
    for a in range(0, n, 5):
        b = min(n, a+5)
        med = lambda x: float(np.nanmedian(x[a:b])) if np.isfinite(x[a:b]).any() else None
        cr = [r for r in accepted if t[a] <= r['t_s'] < t[a]+5]
        timeline.append(dict(t=float(t[a]), speed=med(speed*3.6), hr=med(hr), lat=med(lat), lon=med(lon), distance=float(distance[b-1]/1000),
                             cadence=float(np.median([r['rpm'] for r in cr])) if cr else None,
                             development=float(np.median([r['speed_kmh']/3.6*60/r['rpm'] for r in cr])) if cr else None))
    return _finite(dict(session=sess.path.name, start_utc=sess.meta['start_utc'], gear=sess.gear,
                        summary=summary, findings=findings, timeline=timeline, splits=splits, stops=stops,
                        speed_matched_hr=comparison,
                        methods=[
                            'One-second elapsed-time grid; most recent GPS held for under 10 s and HR for under 5 s. Unknown intervals stay blank and add no distance.',
                            'Distance integrates reported GPS speed, including slow riding, with horizontal accuracy ≤20 m. Reject negative speeds and speeds above 144 km/h. Instantaneous speed can be noisy; integrated distance is an estimate and omitted GPS intervals undercount it.',
                            'Moving means GPS speed ≥3.6 km/h. Stops are known speeds below that threshold for ≥10 s; these can include walking or slow cycling.',
                            'Route requires horizontal GPS accuracy ≤30 m. The map is a local metre projection without online tiles; it is not an elevation or grade measurement.',
                            'HR requires 25–240 bpm and no explicit lost-contact flag. Averages are time weighted. No personal zones, power, calories, or training-load estimates are inferred.',
                            'Cadence is experimental foot-IMU vector-gyro autocorrelation in 8 s windows every 2 s. Require repetition ≥0.6, gyro RMS ≥30 dps, GPS ≥8 km/h, horizontal accuracy ≤20 m and speed accuracy ≤2 m/s. Missing/rejected windows are unclassified. Metres per crank turn = GPS speed ×60 / estimated rpm, a gearing proxy without a crank-sensor reference.',
                            'Charts show 5-second medians. Speed-matched HR compares moving samples before versus after the longest stop ≥2 min (excluding first/last 5%); without such a stop, 10–40% versus 60–90% of elapsed time. Use 5 km/h bands with at least 60 s in each.',
                        ]))


def write_report(path: str | Path | Session, output: str | Path, title: str = 'Cycling ride') -> dict:
    """Write a standalone page and return its underlying analysis."""
    data = analyze(path)
    data['title'] = title
    payload = json.dumps(data, allow_nan=False, separators=(',', ':')).replace('<', '\\u003c').replace('>', '\\u003e').replace('&', '\\u0026')
    template = resources.files('bladetag').joinpath('cycling_report.html').read_text()
    Path(output).write_text(template.replace('__CYCLING_DATA__', payload))
    return data
