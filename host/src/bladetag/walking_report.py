"""Offline multi-session walking report, with explicit sensor coverage."""
from __future__ import annotations

import json
from importlib import resources
from pathlib import Path

import numpy as np

from .session import load_session


def finite(value):
    if isinstance(value, dict):
        return {str(k): finite(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [finite(v) for v in value]
    if isinstance(value, (float, np.floating)):
        return float(value) if np.isfinite(value) else None
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.bool_):
        return bool(value)
    return value


def build(paths, title="Walking · a closer look", progress=None):
    from .report import session_paths
    from .walking_context import analyze_session as context
    from .walking_insights import analyze_session as gait, compare_sessions

    expanded, from_folder = session_paths([str(p) for p in paths])
    sessions, skipped = [], []
    for path in dict.fromkeys(p.resolve() for p in expanded):
        s = load_session(path)
        if s.activity != 'walking':
            if path not in {p.resolve() for p in from_folder}:
                raise ValueError(f'{path.name}: expected a walking session, got {s.activity}')
            skipped.append(dict(id=path.name, reason=f'activity: {s.activity}'))
            continue
        if progress:
            progress(f'Walking: {path.name}')
        sessions.append(dict(id=path.name, start_utc=s.meta['start_utc'], gear=s.gear,
                             notes=s.meta.get('notes', ''), end_reason=s.meta.get('end_reason', ''),
                             context=context(s), gait=gait(s)))
    if not sessions:
        raise ValueError('no sessions labelled walking')
    sessions.sort(key=lambda s: s['start_utc'])
    findings = [f"{len(sessions)} walking recordings are included. Each walk keeps its own sensor coverage; missing tag data does not become zero steps."]
    compared = compare_sessions([dict(session=s['id'], gear=s['gear'], tags=s['gait']['tags']) for s in sessions])
    both = [s for s in sessions if s['gait']['tags'] and s['context']['summary']['hr_coverage'] > 0]
    findings.append(f"Only {len(both)} recording(s) combine usable real foot-tag evidence and heart rate. Most recent sensor-only recordings cannot answer questions about stride mechanics.")
    study = []
    latest = next((s for s in reversed(sessions) if any(t['summary']['n'] >= 100 for t in s['gait']['tags'])), None)
    if latest:
        t = max(latest['gait']['tags'], key=lambda t: t['summary']['n'])
        m = t['summary']
        study.append(f"Latest substantial tagged walk ({latest['id']}): {m['n']} accepted strides, median length {m['stride_m']['median']:.2f} m at {m['speed_kmh']['median']:.2f} km/h. This describes detected intervals, not all steps in the recording.")
        adjusted = next((r for r in compared['sessions'] if r['session'] == latest['id'] and r['group'].endswith(t['position'])), None)
        if adjusted and m['impact_g']['median'] is not None and adjusted['impact_g'] is not None:
            study.append(f"Speed-matched landing comparison: that walk's median landing acceleration is {m['impact_g']['median']:.2f} g overall, versus {adjusted['impact_g']:.2f} g after adjustment to 4.5 km/h. Raw walk averages mix behavior and pace.")
    if compared['groups']:
        lengths = [r['stride_m'] for r in compared['groups'] if r['stride_m'] is not None]
        if lengths:
            study.append(f"At 4.5 km/h, adjusted group stride lengths range from {min(lengths):.2f} to {max(lengths):.2f} m. Review walk counts and between-walk spread before interpreting other differences: single-walk groups cannot establish repeatability or a shoe effect.")
    drifts = [t['half_comparison'] for s in sessions for t in s['gait']['tags'] if t['half_comparison']]
    changes = [(d['late']['stride_m'] / d['early']['stride_m'] - 1) * 100 for d in drifts if d['early']['stride_m']]
    if changes:
        study.append(f"In {len(changes)} walks with comparable early/late gait, speed-adjusted stride length changes range from {min(changes):+.1f}% to {max(changes):+.1f}%. These observational differences do not establish a fatigue trend.")
    comparison = dict(rows=compared['groups'], sessions=compared['sessions'],
                      description='Adjusted to 4.5 km/h using strides within ±0.5 km/h, with a separate linear speed adjustment in each walk. At least 20 gait-valid strides per walk. Groups separate labelled feet and weight each walk equally; single-walk groups cannot estimate repeatability.')
    methods = ['Walking context: exact interval integration; GPS and HR held forward at most 5 seconds, never across longer gaps. GPS position accuracy ≤20 m, speed 0–18 km/h, speed accuracy ≤1.5 m/s when supplied. HR 25–240 bpm with no explicit contact loss. Moving means phone speed ≥1.8 km/h; pauses require at least 10 seconds below it.',
               'Foot tag: simulated tags excluded; pre-start and post-end samples removed. Clock scale must be within 2% of expected, and finite receiver-fit residual must be ≤1 second. Saturation-touching strides excluded; sample gaps split integration. Native planted-foot detector accepts stride periods 0.7–1.8 seconds, so very slow/irregular walking can be missed.',
               'Crash sessions without an end use the final real sensor observation as their duration; no trailing interval is invented. Per-stride quartiles describe variability, not statistical uncertainty.']
    return finite(dict(title=title, sessions=sessions, skipped=skipped, findings=findings,
                       study_findings=study, comparison=comparison, methods=methods,
                       limitations=[
                           'A stride runs from one plant of the tagged foot to the next. Steps/min assumes alternating feet; step length is half a stride, not a measured left or right step.',
                           'Foot height, pitch and landing acceleration are sensor estimates. Mounting, turns, surface and pace can change them. Landing g is not ground force or a clinical assessment.',
                           'Detected strides describe accepted walking intervals only. Shopping, shuffling, very slow steps, missing samples and pauses can be excluded. Summed strides are not a whole-walk distance.',
                           'Shoe comparisons are observational. Speed adjustment cannot remove route, surface, day, foot or mounting differences. Repeated walks are the comparison unit, not thousands of independent strides.',
                           'GPS and heart-rate gaps remain missing. Route points are approximate phone locations. Correlation and early/late differences do not establish fatigue or cause.'
                       ]))


def write_report(paths, output, title="Walking · a closer look", progress=None):
    report = build(paths, title, progress)
    encoded = json.dumps(report, allow_nan=False, separators=(',', ':')).replace('<', '\\u003c').replace('>', '\\u003e').replace('&', '\\u0026')
    template = resources.files('bladetag').joinpath('walking_report.html').read_text()
    Path(output).write_text(template.replace('__WALKING_DATA__', encoded), encoding='utf-8')
    return report
