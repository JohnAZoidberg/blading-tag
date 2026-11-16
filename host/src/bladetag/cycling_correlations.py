"""Descriptive ride associations, aggregated to avoid counting overlapping windows."""
import numpy as np
from scipy.stats import spearmanr


def behavior_correlations(timeline, stops):
    """Return disjoint 30 s moving blocks and pooled/leg rank correlations.

    Blocks require at least five paired 5 s speed/HR bins at >=8 km/h.
    Cadence additionally needs three accepted bins. No gap interpolation.
    """
    blocks = []
    for start in range(0, int(max((p['t'] for p in timeline), default=-1))+1, 30):
        rows = [p for p in timeline if start <= p['t'] < start+30
                and p['speed'] is not None and p['speed'] >= 8 and p['hr'] is not None]
        if len(rows) < 5:
            continue
        cad = [p['cadence'] for p in rows if p.get('cadence') is not None]
        blocks.append(dict(t=start, speed=float(np.mean([p['speed'] for p in rows])),
                           hr=float(np.mean([p['hr'] for p in rows])),
                           cadence=float(np.median(cad)) if len(cad) >= 3 else None))
    groups = [('All moving blocks', blocks)]
    pause = max(stops, key=lambda s: s['duration_s'], default=None)
    if pause and pause['duration_s'] >= 120:
        groups += [('Before longest break', [b for b in blocks if b['t']+30 <= pause['start_s']]),
                   ('After longest break', [b for b in blocks if b['t'] >= pause['end_s']])]
    results = []
    for label, group in groups:
        for x, y in [('speed', 'hr'), ('cadence', 'hr'), ('cadence', 'speed')]:
            pairs = [(b[x], b[y]) for b in group if b[x] is not None and b[y] is not None]
            r = None
            if len(pairs) >= 10:
                a = np.asarray(pairs)
                if np.ptp(a[:, 0]) > 0 and np.ptp(a[:, 1]) > 0:
                    r = float(spearmanr(a[:, 0], a[:, 1]).statistic)
            results.append(dict(group=label, x=x, y=y, n=len(pairs), r=r))
    return dict(blocks=blocks, correlations=results)
