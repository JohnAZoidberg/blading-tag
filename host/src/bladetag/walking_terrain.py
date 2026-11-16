"""Exploratory stair candidates, never a validated terrain classifier.

Repeated vertical changes with short horizontal strides are stronger evidence
than one displaced stride. A second stance detector admits shorter, less quiet
plants because descending stairs can fail the usual walking detector. These
extra estimates are used only for candidate discovery, never gait averages.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .explore import sample_frame, still_mask
from .timesync import fit_clocks
from .walking import Params, strides

RULES = ('Candidate strides: horizontal distance 0.25–1.05 m, vertical change '
         '0.14–0.50 m in either direction, vertical/horizontal ratio ≥0.20; '
         'at least four same-direction strides, no more than 3 s between '
         'successive plants. These heuristic thresholds are unvalidated; agreement between detector settings is sensitivity evidence, not validation. A four-stride minimum can miss a short staircase. '
         'Vertical integration error, slopes and mounting can imitate stairs. '
         'Relaxed plants use a 30 ms window, gyro <100 °/s, acceleration within '
         '0.4 g of gravity, and 0.7–3 s stride periods. Relaxed-only candidates '
         'have lower confidence. GPS gaps suggest indoor transitions, not store identity.')


def candidate_runs(frame):
    """Return sustained same-sign vertical-motion bouts from stride records."""
    if frame.empty:
        return []
    df = frame.sort_values('t_s')
    bouts, run = [], []
    def finish():
        if len(run) < 4:
            return
        g = pd.DataFrame(run)
        bouts.append(dict(start_s=float(g.t_s.iloc[0]), end_s=float((g.t_s+g.period_s).iloc[-1]),
                          direction='up' if g.dz_m.median() > 0 else 'down', n=len(g),
                          median_vertical_m=float(g.dz_m.median()),
                          median_horizontal_m=float(g.stride_m.median()),
                          median_period_s=float(g.period_s.median()),
                          median_swing_cm=float(g.lift_cm.median()) if 'lift_cm' in g and g.lift_cm.notna().any() else None))
    for row in df.to_dict('records'):
        z, x = row['dz_m'], row['stride_m']
        eligible = .25 <= x <= 1.05 and .14 <= abs(z) <= .50 and abs(z)/x >= .20
        if not eligible:
            finish(); run = []
            continue
        if run and (np.sign(z) != np.sign(run[-1]['dz_m']) or row['t_s']-run[-1]['t_s'] > 3):
            finish(); run = []
        run.append(row)
    finish()
    return bouts


def gps_gaps(sess):
    if sess.gps is None or len(sess.gps) < 2:
        return []
    t = np.sort((sess.gps['elapsed_ns']-sess.start_elapsed_ns)/1e9)
    return [dict(start_s=float(a), end_s=float(b), duration_s=float(b-a))
            for a, b in zip(t[:-1], t[1:]) if b-a >= 120 and a >= 0]


def analyze_session(sess):
    gaps, tags = gps_gaps(sess), []
    end = sess.meta.get('end_elapsed_ns')
    duration = (int(end)-sess.start_elapsed_ns)/1e9 if end is not None else np.inf
    for tag in sess.tags:
        if tag.meta.simulated or not len(tag.sample_index):
            continue
        clock = fit_clocks(tag)
        if abs(clock.rx_clock.b/1000-1) > .02 or (np.isfinite(clock.rx_residual_ms) and clock.rx_residual_ms > 1000):
            continue
        df = sample_frame(sess, tag, clock)
        df = df[(df.t_s >= 0) & (df.t_s <= duration)].reset_index(drop=True)
        quiet = still_mask(df, clock.rate_hz)
        windows = []
        for gap in gaps:
            for boundary, center in [('GPS loss / possible entry', gap['start_s']), ('GPS return / possible exit', gap['end_s'])]:
                # GPS loss can lag a door by minutes. Quiet feet cannot distinguish
                # a stationary person from transport at a steady velocity.
                lo, hi = max(0., center-120), min(duration, center+120)
                mask = (df.t_s >= lo) & (df.t_s <= hi)
                if mask.any():
                    windows.append(dict(boundary=boundary, center_s=center, start_s=lo, end_s=hi,
                                        quiet_fraction=float(quiet[mask].mean()),
                                        observed_s=float(mask.sum()/clock.rate_hz)))
        detections = []
        for name, params in [('standard', Params()), ('shorter_plants', Params(stance_win_s=.03)),
                             ('relaxed', Params(stance_win_s=.03, stance_gyro_dps=100, stance_acc_g=.4,
                                                stance_vib_g=.3, max_period_s=3))]:
            st = strides(df, clock.rate_hz, p=params)
            # Discard any integration touching a clipped sample.
            if len(st):
                clip = (df.a_clip | df.g_clip).to_numpy()
                total = np.r_[0, np.cumsum(clip)]
                a = np.searchsorted(df.t_s, st.t_s)
                b = np.searchsorted(df.t_s, st.t_s+st.period_s, side='right')
                st = st[(total[b]-total[a]) == 0]
            detections.extend(dict(**r, detector=name) for r in candidate_runs(st))
        # Merge overlapping same-direction detections; retain per-detector evidence.
        merged = []
        for r in sorted(detections, key=lambda r: r['start_s']):
            match = next((m for m in merged if m['direction'] == r['direction'] and
                          r['start_s'] <= m['end_s']+3 and r['end_s'] >= m['start_s']-3), None)
            if match:
                match['start_s'] = min(match['start_s'], r['start_s'])
                match['end_s'] = max(match['end_s'], r['end_s'])
                match['evidence'].append(r)
            else:
                merged.append(dict(start_s=r['start_s'], end_s=r['end_s'], direction=r['direction'], evidence=[r]))
        for m in merged:
            names = sorted({r['detector'] for r in m['evidence']})
            m['support'] = names
            m['confidence'] = 'parameter-stable; terrain unresolved' if 'standard' in names and len(names) >= 2 else 'possible; detector-sensitive'
            m['near_gap'] = []
            for gap in gaps:
                if abs(m['end_s']-gap['start_s']) <= 45 or abs(m['start_s']-gap['start_s']) <= 45:
                    m['near_gap'].append(dict(boundary='GPS loss / possible entry', **gap))
                if abs(m['end_s']-gap['end_s']) <= 45 or abs(m['start_s']-gap['end_s']) <= 45:
                    m['near_gap'].append(dict(boundary='GPS return / possible exit', **gap))
        tags.append(dict(position=tag.meta.position, address=tag.meta.address, candidates=merged, transition_windows=windows))
    return dict(tags=tags, gps_gaps=gaps, rules=RULES,
                interpretation='Vertical-motion candidates: stairs, hills and integration error remain possible. Unflagged intervals may still contain stairs, escalators or slopes. Quiet-foot windows cannot distinguish standing still from standing on an escalator or elevator. GPS altitude is not used: it is too noisy here to validate individual flights.')
