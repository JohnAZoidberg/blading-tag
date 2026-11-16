"""Walking report evidence, retaining per-foot estimates and their limits.

Detected strides are a subset of walking, not a whole-session step counter.
Speed is derived from the same integration as length, so associations with
speed are descriptive rather than independent validation.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import walking
from .explore import sample_frame
from .timesync import fit_clocks


def _clean(value):
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    if isinstance(value, (float, np.floating)):
        return float(value) if np.isfinite(value) else None
    if isinstance(value, np.integer):
        return int(value)
    return value


def summarize(st):
    """Distribution of the accepted strides, without treating them as trials."""
    result = {'n': len(st)}
    for c in ['stride_m', 'step_m', 'period_s', 'speed_kmh', 'cadence_spm', 'dz_m', *walking.GAIT_COLUMNS]:
        a = st[c].dropna() if c in st else pd.Series(dtype=float)
        result[c] = dict(median=a.median(), q25=a.quantile(.25), q75=a.quantile(.75)) if len(a) else dict(median=None, q25=None, q75=None)
    result['detected_distance_m'] = float(st['stride_m'].sum())
    result['detected_time_s'] = float(st['period_s'].sum())
    return _clean(result)


def analyze_session(sess):
    """Return real tags only; buffered pre-start samples cannot enter strides."""
    tags = []
    findings = []
    skipped = sum(t.meta.simulated for t in sess.tags)
    if skipped:
        findings.append(f'{skipped} simulated tag(s) excluded from walking evidence.')
    end = sess.meta.get('end_elapsed_ns')
    duration = (int(end) - sess.start_elapsed_ns) / 1e9 if end else None
    for tag in sess.tags:
        if tag.meta.simulated:
            continue
        if not len(tag.sample_index):
            findings.append(f'{tag.meta.position}: no IMU samples.')
            continue
        clock = fit_clocks(tag)
        if abs(clock.rx_clock.b / 1000 - 1) > .02 or (np.isfinite(clock.rx_residual_ms) and clock.rx_residual_ms > 1000):
            findings.append(f'{tag.meta.position}: tag-to-phone clock fit is unreliable (residual {clock.rx_residual_ms / 1000:.1f} s, clock scale {clock.rx_clock.b / 1000:.3f}); timed gait estimates excluded because session boundaries and periods are uncertain.')
            continue
        df = sample_frame(sess, tag, clock)
        df = df[df.t_s >= 0]
        if duration is not None:
            df = df[df.t_s <= duration]
        df = df.reset_index(drop=True)
        if len(df) < 2:
            findings.append(f'{tag.meta.position}: no IMU samples within session boundaries.')
            continue
        raw = walking.strides(df, clock.rate_hz)
        # Reject an estimate touching sensor saturation. No numerical length
        # or height cutoff: retain unusual strides and expose height residuals.
        starts = np.searchsorted(df.t_s, raw.t_s)
        ends = np.searchsorted(df.t_s, raw.t_s + raw.period_s, side='right')
        clip = (df.a_clip | df.g_clip).to_numpy()
        total = np.r_[0, np.cumsum(clip)]
        clipped = (total[ends] - total[starts]) > 0
        st = raw[~clipped].copy()
        st['cadence_spm'] = 120 / st.period_s
        windows = []
        for minute, part in st.groupby((st.t_s // 60).astype(int)):
            windows.append(dict(start_s=int(minute) * 60, **summarize(part)))
        bins = []
        for speed_bin, part in st.groupby((st.speed_kmh // .5).astype(int)):
            if len(part) >= 20:
                bins.append(dict(speed_from_kmh=float(speed_bin) * .5, speed_to_kmh=float(speed_bin + 1) * .5, **summarize(part)))
        drift = None
        if len(st) >= 80:
            midpoint = (float(df.t_s.min()) + float(df.t_s.max())) / 2
            early, late = st[st.t_s < midpoint], st[st.t_s >= midpoint]
            if len(early) and len(late):
                lo = max(early.speed_kmh.quantile(.1), late.speed_kmh.quantile(.1))
                hi = min(early.speed_kmh.quantile(.9), late.speed_kmh.quantile(.9))
                if hi > lo:
                    speed = float((lo + hi) / 2)
                    band = float(min(.5, (hi - lo) / 2))
                    a, b = walking.at_speed(early, speed, band), walking.at_speed(late, speed, band)
                    if min(a['n'], b['n']) >= 20:
                        drift = dict(midpoint_s=midpoint, speed_kmh=speed, band_kmh=band, early=a, late=b)
        local = []
        if len(st):
            med = st.median(numeric_only=True)
            local.append(f'{len(st)} detected strides: median length {med.stride_m:.2f} m, estimated cadence {med.cadence_spm:.0f} steps/min and speed {med.speed_kmh:.1f} km/h.')
            residual = float(st.dz_m.abs().median())
            local.append(f'Median absolute estimated vertical change is {100 * residual:.1f} cm per stride; terrain and integration error both contribute.')
        else:
            local.append('No usable planted-foot strides were detected; this does not mean no walking occurred.')
        if clipped.any():
            local.append(f'{int(clipped.sum())} stride estimates touching sensor saturation were excluded.')
        quality = dict(samples=len(df), rate_hz=clock.rate_hz, segments=int(df.segment.nunique()),
                       clipped_samples=int(clip.sum()), clipped_strides=int(clipped.sum()), raw_strides=len(raw),
                       sample_start_s=float(df.t_s.min()), sample_end_s=float(df.t_s.max()),
                       observed_sample_s=len(df) / clock.rate_hz,
                       clock_residual_ms=clock.rx_residual_ms, receiver_clock_scale=clock.rx_clock.b / 1000)
        tags.append(dict(position=tag.meta.position, address=tag.meta.address, summary=summarize(st),
                         quality=quality, strides=st.to_dict('records'), minutes=windows,
                         speed_bins=bins, half_comparison=drift, findings=local))
    if not tags:
        findings.append('No real foot-tag evidence: gait, stride length and step count are unavailable.')
    return _clean(dict(tags=tags, findings=findings, simulated_tags_excluded=skipped))


def compare_sessions(entries, speed_kmh=4.5, band_kmh=.5):
    """Descriptive same-foot shoe comparisons, each session weighted equally.

    entries: dictionaries with session, gear and analyze_session's tags.
    A session needs 20 gait strides in the target band. Report group sample
    size and between-session SD, never stride-level inferential statistics.
    """
    runs = []
    for entry in entries:
        for tag in entry['tags']:
            st = pd.DataFrame(tag['strides'])
            if st.empty:
                continue
            runs.append((f"{entry.get('gear') or 'Unlabelled'} · {tag['position']}", entry['session'], st))
    if not runs:
        return dict(speed_kmh=speed_kmh, band_kmh=band_kmh, sessions=[], groups=[])
    per, groups, _ = walking.compare(runs, speed_kmh, band_kmh)
    per = per[per.n >= 20]
    groups = groups[groups.sessions > 0]
    return _clean(dict(speed_kmh=speed_kmh, band_kmh=band_kmh,
                       sessions=per.to_dict('records'), groups=groups.to_dict('records'),
                       interpretation='Speed-adjusted descriptions, grouped by labelled foot. Different days, routes and unrecorded mounting changes prevent attributing differences to shoes. No simultaneous bilateral comparison is available.'))
