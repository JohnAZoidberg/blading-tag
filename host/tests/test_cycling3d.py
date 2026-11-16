import json
from types import SimpleNamespace

import numpy as np
import pytest

from bladetag import cycling3d
from bladetag.session import Session


def test_periodic_integration_recovers_ellipse_without_forcing_circle():
    n, period = 128, .85
    phase = np.arange(n)*2*np.pi/n
    expected = np.column_stack([.12*np.cos(phase),.02*np.sin(phase),.18*np.sin(phase)])
    acceleration = -(2*np.pi/period)**2*expected + [2.,-1.,9.80665]
    path = cycling3d.periodic_path(acceleration,period)
    assert np.allclose(path,expected,atol=1e-10)
    # Constant bias and velocity have no observable periodic displacement.
    assert np.allclose(path.mean(axis=0),0,atol=1e-10)
    assert np.ptp(path[:,2])/np.ptp(path[:,0]) == pytest.approx(1.5)


def test_high_frequency_vibration_is_not_integrated_into_pedal_path():
    n, period = 128, 1.
    phase = np.arange(n)*2*np.pi/n
    acceleration = np.column_stack([np.cos(phase),np.sin(phase),np.cos(phase)])
    baseline = cycling3d.periodic_path(acceleration,period)
    acceleration += 40*np.sin(20*phase)[:,None]
    assert np.allclose(cycling3d.periodic_path(acceleration,period),baseline)


def fake_session(tmp_path):
    fs, origin = 100., 1_000_000_000_000
    times = np.arange(-5,26,1/fs)
    # A real sample-index gap that must prevent a loop crossing it.
    times = times[(times<11)|(times>=13)]
    idx = np.rint((times+5)*fs).astype(int)
    omega = 2*np.pi*70/60
    acc = np.column_stack([-.14*omega**2*np.cos(omega*times),
                           times*0,9.80665-.18*omega**2*np.sin(omega*times)])/9.80665
    tag = SimpleNamespace(meta=SimpleNamespace(position='left_foot'),sample_index=idx,
                          accel_g=acc,gyro_dps=np.zeros_like(acc))
    clock = SimpleNamespace(rate_hz=fs,sample_elapsed_ns=lambda indices:origin+(indices/fs-5)*1e9)
    sess = Session(tmp_path,dict(start_elapsed_ns=origin,end_elapsed_ns=origin+25e9,
                                start_utc='2026-11-16T00:00:00Z',activity='cycling'),[tag],None)
    return sess,clock


def test_build_closed_loops_skip_gaps_and_keep_real_scale(tmp_path,monkeypatch):
    sess,clock = fake_session(tmp_path)
    monkeypatch.setattr(cycling3d,'fit_clocks',lambda _:clock)
    monkeypatch.setattr(cycling3d,'cadence_track',lambda _:[dict(t_s=t,rpm=70.,strength=.9,speed_kmh=25.) for t in [5.,12.,20.]])
    data = cycling3d.build(sess)
    assert len(data['cycles']) == 2
    assert set(data['typical']) == {'all','early','late'}
    for cycle in data['cycles']:
        pos,q = np.array(cycle['pos']),np.array(cycle['quat'])
        assert pos.shape == (65,3)
        assert np.allclose(pos[0],pos[-1])
        assert np.allclose(q[0],q[-1])
        assert np.allclose(np.linalg.norm(q,axis=1),1,atol=2e-6)
        assert 0 <= cycle['t_s'] <= 25
        assert cycle['metrics']['vertical_cm'] == pytest.approx(36,rel=.2)
        assert cycle['metrics']['fore_aft_cm'] == pytest.approx(28,rel=.2)
        assert cycle['hr_bpm'] is None
    json.dumps(data,allow_nan=False)


def test_missing_foot_or_reliable_pedals_explains_failure(tmp_path,monkeypatch):
    sess,_ = fake_session(tmp_path)
    monkeypatch.setattr(cycling3d,'cadence_track',lambda _:[])
    with pytest.raises(ValueError,match='No reliable moving'):
        cycling3d.build(sess)
    sess.tags=[]
    with pytest.raises(ValueError,match='labelled'):
        cycling3d.build(sess)


def test_rotating_tag_recovers_known_ellipse_and_relative_rotation():
    from scipy.spatial.transform import Rotation
    fs=100.
    t=np.arange(800)/fs
    omega=2*np.pi*70/60
    phase=omega*t
    angle=.35*np.sin(phase)
    rotation=Rotation.from_rotvec(np.column_stack([t*0,angle,t*0]))
    world=np.column_stack([-.14*omega**2*np.cos(phase),t*0,
                           9.80665-.18*omega**2*np.sin(phase)])
    acc=rotation.inv().apply(world)/9.80665
    gyro=np.column_stack([t*0,np.degrees(.35*omega*np.cos(phase)),t*0])
    cycle=cycling3d._cycle(acc,gyro,fs,4.,60/70,64)
    assert cycle is not None
    assert cycle['metrics']['fore_aft_cm'] == pytest.approx(28,rel=.1)
    assert cycle['metrics']['vertical_cm'] == pytest.approx(36,rel=.1)
    assert cycle['metrics']['relative_tilt_deg'] == pytest.approx(np.degrees(.35),abs=2)


def test_html_embeds_data_safely(tmp_path,monkeypatch):
    monkeypatch.setattr(cycling3d,'build',lambda _:dict(cycles=[],summary={}))
    output=tmp_path/'replay.html'
    data=cycling3d.write_report('unused',output,'</script><script>alert(1)</script>')
    html=output.read_text()
    assert '__CYCLING3D_DATA__' not in html
    assert '</script><script>alert(1)' not in html
    assert '\\u003c/script' in html
    assert 'https://' not in html
    assert data['title'].startswith('</script>')
