"""Offline walking replay using the same plant-to-plant integration as walking analysis."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
from scipy.spatial.transform import Rotation, Slerp

from . import walking
from .explore import sample_frame
from .session import Session, load_session
from .timesync import fit_clocks


def build(path, *, tag=None, max_strides=None, frames=64):
    if (max_strides is not None and max_strides < 1) or frames < 16:
        raise ValueError('max_strides must be positive or None and frames at least 16')
    sess = path if isinstance(path, Session) else load_session(path)
    if sess.activity not in ('walking', 'unknown'):
        raise ValueError('Walking replay requires a walking session')
    feet = [t for t in sess.tags if t.meta.position in ('left_foot', 'right_foot')
            and (tag is None or tag in (t.meta.position, t.meta.address))]
    if not feet:
        raise ValueError('Walking replay needs a tag labelled left_foot or right_foot')
    foot = feet[0]
    clock = fit_clocks(foot)
    df = sample_frame(sess, foot, clock)
    trajectories = []
    rows = walking.strides(df, clock.rate_hz, trajectories=trajectories)
    cycles = []
    for row, (t, pos, rs) in zip(rows.to_dict('records'), trajectories):
        if not np.isfinite(pos).all() or row['stride_m'] < .2 or row['stride_m'] > 3:
            continue
        # Forward follows displacement; each plant starts a new local heading.
        f = pos[-1, :2] / row['stride_m']
        basis = np.array([[f[0], f[1], 0], [-f[1], f[0], 0], [0, 0, 1.]])
        pos = pos @ basis.T
        pos[:, 0] -= pos[-1, 0] / 2
        relative = basis @ rs @ rs[0].T @ basis.T
        u = np.linspace(t[0], t[-1], frames + 1)
        p = np.column_stack([np.interp(u, t, pos[:, k]) for k in range(3)])
        q = Slerp(t, Rotation.from_matrix(relative))(u).as_quat()
        metrics = dict(fore_aft_cm=float(np.ptp(p[:, 0])*100),
                       lateral_cm=float(np.ptp(p[:, 1])*100),
                       vertical_cm=float(np.ptp(p[:, 2])*100))
        cycles.append(dict(t_s=row['t_s'], period_s=row['period_s'], rpm=120/row['period_s'],
                           speed_kmh=row['speed_kmh'], hr_bpm=None, metrics=metrics,
                           pos=p.round(5).tolist(), quat=q.round(6).tolist()))
    if not cycles:
        raise ValueError('No complete walking strides passed reconstruction checks')
    accepted = len(cycles)
    # Keep all accepted strides by default so long bouts remain continuous.
    # An explicit limit retains a prefix, never samples holes into a bout.
    if max_strides is not None:
        cycles = cycles[:max_strides]
    bout = -1
    previous = None
    for c in cycles:
        contiguous = previous is not None and abs(c['t_s'] - previous['t_s'] - previous['period_s']) < .03
        if not contiguous:
            bout += 1
        c['bout'] = bout
        previous = c
    def typical(part):
        p = np.median([c['pos'] for c in part], axis=0)
        return dict(pos=p.round(5).tolist(), quat=part[len(part)//2]['quat'],
                    rpm=float(np.median([c['rpm'] for c in part])),
                    metrics={k: float(np.median([c['metrics'][k] for c in part])) for k in part[0]['metrics']})
    k = max(1, len(cycles)//5)
    return dict(session=sess.path.name, side=foot.meta.position,
                duration_s=float(df.t_s.iloc[-1]), cycles=cycles,
                typical=dict(all=typical(cycles), early=typical(cycles[:k]), late=typical(cycles[-k:])),
                methods=[
                    f'{accepted} accepted plant-to-plant strides; {len(cycles)} retained for replay. Each stride is two steps of the tagged foot cycle. Only the labelled {foot.meta.position} tag is shown.',
                    'Gyro orientation and gravity-corrected acceleration are integrated between quiet foot plants. Velocity error is removed linearly to make velocity zero at the next plant. Sample gaps are never bridged.',
                    'Forward is the measured horizontal displacement of each stride. Paths retain their measured scale and height error; absolute heading is unavailable. The path is open, from one plant to the next; consecutive strides are placed end-to-end along a straight local walking lane. Actual route curvature is unknown. The camera follows the tag with a short trail. Playback resets only between interrupted bouts or at the end of the recording.',
                    'The dot is the tag, not the toe. The shoe has an illustrative neutral mount at the first plant; relative rotation is measured. A shin-mounted tag shows shin motion, not anatomical shoe angles.',
                    'Early and late are the first and last 20% of retained strides. Typical paths are phase-wise medians with representative stride rotation. Speed, terrain and mounting can affect differences.',
                ])


def write_report(path, output, title='Walking stride replay', *, tag=None):
    data = build(path, tag=tag)
    data['title'] = title
    payload = json.dumps(data, allow_nan=False, separators=(',', ':')).replace('<', '\\u003c').replace('>', '\\u003e').replace('&', '\\u0026')
    template = Path(__file__).with_name('walking3d_template.html').read_text()
    Path(output).write_text(template.replace('__WALKING3D_DATA__', payload))
    return data
