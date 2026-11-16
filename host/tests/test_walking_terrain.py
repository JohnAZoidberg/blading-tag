import pandas as pd

from bladetag.walking_terrain import candidate_runs


def frame(z, x=.6):
    return pd.DataFrame([dict(t_s=i*1.2, period_s=1.2, dz_m=v, stride_m=x, lift_cm=22.) for i,v in enumerate(z)])


def test_requires_sustained_same_direction_short_strides():
    assert not candidate_runs(frame([.3,.01,.3,.01,.3,.01]))
    assert not candidate_runs(frame([.3]*8, x=1.6))
    assert not candidate_runs(frame([.3,-.3,.3,-.3,.3,-.3]))
    up, down = candidate_runs(frame([.3]*5+[-.25]*5))
    assert up['direction'] == 'up' and up['n'] == 5
    assert down['direction'] == 'down' and down['n'] == 5


def test_missing_intervals_and_implausible_displacements_break_runs():
    df = frame([.3]*6)
    df.loc[3:, 't_s'] += 20
    assert not candidate_runs(df)
    assert not candidate_runs(frame([.8]*10))
    assert candidate_runs(pd.DataFrame()) == []


def test_pipeline_relaxed_only_clipping_and_gps_boundary(monkeypatch):
    from types import SimpleNamespace as NS
    import json
    import numpy as np
    from bladetag import walking_terrain as terrain

    df = pd.DataFrame(dict(t_s=np.arange(0., 300., .01), a_clip=False, g_clip=False))
    clock = NS(rx_clock=NS(b=1000.), rx_residual_ms=5., rate_hz=100.)
    tag = NS(meta=NS(simulated=False, address='real', position='left_foot'), sample_index=np.arange(len(df)))
    gps = np.array([(100e9,), (250e9,)], dtype=[('elapsed_ns','f8')])
    sess = NS(tags=[tag], gps=gps, start_elapsed_ns=0, meta=dict(end_elapsed_ns=300e9))
    monkeypatch.setattr(terrain, 'fit_clocks', lambda _: clock)
    monkeypatch.setattr(terrain, 'sample_frame', lambda *args: df)
    monkeypatch.setattr(terrain, 'still_mask', lambda *args: np.ones(len(df), dtype=bool))
    st = frame([-.25]*8)
    st.t_s += 85
    monkeypatch.setattr(terrain, 'strides', lambda *args, p: st if p.stance_gyro_dps == 100 else st.iloc[:0])
    result = terrain.analyze_session(sess)
    candidate, = result['tags'][0]['candidates']
    assert candidate['direction'] == 'down'
    assert candidate['confidence'] == 'possible; detector-sensitive'
    assert candidate['near_gap'][0]['boundary'] == 'GPS loss / possible entry'
    json.dumps(result, allow_nan=False)
    df['a_clip'] = True
    assert not terrain.analyze_session(sess)['tags'][0]['candidates']
    tag.meta.simulated = True
    assert not terrain.analyze_session(sess)['tags']
