import json
from types import SimpleNamespace

import numpy as np
import pytest

from bladetag import walking3d
from bladetag.session import Session
from test_walking import synthetic_walk


def test_replay_preserves_stride_displacement_and_skips_gaps(tmp_path, monkeypatch):
    df, fs = synthetic_walk()
    df.loc[df.t_s > 12, 'segment'] = 1
    foot = SimpleNamespace(meta=SimpleNamespace(position='right_foot', address='test'))
    sess = Session(tmp_path, dict(start_elapsed_ns=0, activity='walking'), [foot], None)
    monkeypatch.setattr(walking3d, 'fit_clocks', lambda _: SimpleNamespace(rate_hz=fs))
    monkeypatch.setattr(walking3d, 'sample_frame', lambda *args: df)
    data = walking3d.build(sess, max_strides=10)
    assert len(data['cycles']) == 10
    for a, b in zip(data['cycles'], data['cycles'][1:]):
        if a['bout'] == b['bout']:
            assert b['t_s'] == pytest.approx(a['t_s']+a['period_s'], abs=.03)
    assert max(sum(c['bout'] == b['bout'] for c in data['cycles']) for b in data['cycles']) <= 6
    for c in data['cycles']:
        p, q = np.array(c['pos']), np.array(c['quat'])
        assert p.shape == (65, 3)
        assert p[-1, 0]-p[0, 0] == pytest.approx(1.4, rel=.04)
        assert np.linalg.norm(q, axis=1) == pytest.approx(np.ones(65), abs=2e-6)
        assert c['rpm'] == pytest.approx(120/c['period_s'])
        assert not c['t_s'] < 12 < c['t_s']+c['period_s']
    json.dumps(data, allow_nan=False)
    monkeypatch.setattr(walking3d, 'build', lambda *a, **kw: data.copy())
    out = tmp_path/'replay.html'
    walking3d.write_report(sess, out, '</script><script>alert(1)</script>')
    html = out.read_text()
    assert '__WALKING3D_DATA__' not in html
    assert '</script><script>alert(1)' not in html
    assert 'https://' not in html


def test_missing_foot(tmp_path):
    sess = Session(tmp_path, dict(start_elapsed_ns=0, activity='walking'), [], None)
    with pytest.raises(ValueError, match='labelled'):
        walking3d.build(sess)
