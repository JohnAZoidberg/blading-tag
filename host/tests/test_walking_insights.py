import json
from types import SimpleNamespace as NS

import numpy as np
import pytest

from bladetag import walking_insights as w
from bladetag.timesync import ClockFit, LinearMap
from test_walking import synthetic_walk


@pytest.fixture
def evidence(monkeypatch):
    df, fs = synthetic_walk(realistic=True, n_strides=50)
    df['a_clip'] = False
    df['g_clip'] = False
    tag = NS(meta=NS(simulated=False, position='right_foot', address='real'), sample_index=np.arange(len(df)))
    sess = NS(tags=[tag], meta={}, start_elapsed_ns=0)
    monkeypatch.setattr(w, 'sample_frame', lambda *a: df)
    monkeypatch.setattr(w, 'fit_clocks', lambda *a: ClockFit(LinearMap(0, 5000), LinearMap(0, 1000), fs, 1))
    return sess, df, fs


def test_real_strides_saturation_and_boundaries(evidence):
    sess, df, fs = evidence
    result = w.analyze_session(sess)
    before = result['tags'][0]['summary']['n']
    assert before > 40
    assert all(r['cadence_spm'] == pytest.approx(120/r['period_s']) for r in result['tags'][0]['strides'])
    json.dumps(result, allow_nan=False)
    df.loc[1000:1200, 'a_clip'] = True
    after = w.analyze_session(sess)['tags'][0]
    assert after['quality']['clipped_strides'] > 0
    assert after['summary']['n'] < before
    sess.meta['end_elapsed_ns'] = 10_000_000_000
    bounded = w.analyze_session(sess)['tags'][0]['strides']
    assert all(r['t_s'] >= 0 and r['t_s']+r['period_s'] <= 10 for r in bounded)


@pytest.mark.parametrize('scale,residual', [(440, 1), (1000, 2000)])
def test_bad_clocks_excluded(evidence, monkeypatch, scale, residual):
    sess, _, fs = evidence
    monkeypatch.setattr(w, 'fit_clocks', lambda *a: ClockFit(LinearMap(0, 5000), LinearMap(0, scale), fs, residual))
    assert not w.analyze_session(sess)['tags']


def test_simulated_tags_and_same_foot_comparison(evidence):
    sess, _, _ = evidence
    tags = w.analyze_session(sess)['tags']
    c = w.compare_sessions([dict(session='a', gear='shoe', tags=tags), dict(session='b', gear='shoe', tags=tags)], 4.5, 2)
    assert c['groups'][0]['sessions'] == 2
    assert 'right_foot' in c['groups'][0]['group']
    json.dumps(c, allow_nan=False)
    sess.tags[0].meta.simulated = True
    assert w.analyze_session(sess)['simulated_tags_excluded'] == 1
    assert not w.analyze_session(sess)['tags']
