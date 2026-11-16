import json

import numpy as np
import pytest

from bladetag.cycling import analyze, write_report
from bladetag.session import GPS_COLUMNS, Session


def fixture_session(tmp_path, duration=20):
    origin = 1_000_000_000_000
    gps = np.full(3, np.nan, dtype=[(c, 'f8') for c in GPS_COLUMNS])
    gps['elapsed_ns'] = origin + np.array([0, 5, 15])*1e9
    gps['speed_mps'] = [2, 0, 4]
    gps['lat'], gps['lon'], gps['h_acc_m'] = 25, 121, 5
    rows = [dict(elapsed_ns=origin, bpm=100, contact_detected=True),
            dict(elapsed_ns=origin+5_000_000_000, bpm=0, contact_detected=True),
            dict(elapsed_ns=origin+10_000_000_000, bpm=150, contact_detected=False),
            dict(elapsed_ns=origin+15_000_000_000, bpm=120, contact_detected=True)]
    return Session(tmp_path, dict(start_elapsed_ns=origin, end_elapsed_ns=origin+duration*1e9,
                                  start_utc='2026-11-16T00:00:00Z', activity='cycling'), [], gps, heart_rate=rows)


def test_time_weighted_speed_hr_and_stops(tmp_path):
    result = analyze(fixture_session(tmp_path))
    s = result['summary']
    assert s['distance_km'] == pytest.approx(.03)
    assert s['moving_s'] == 10
    assert s['stopped_s'] == 10
    assert s['mean_hr_bpm'] == 110
    assert s['hr_coverage'] == .5
    assert result['stops'] == [dict(start_s=5., end_s=15., duration_s=10.)]
    assert sum(x['distance_km'] for x in result['splits']) == pytest.approx(.03)


def test_gaps_do_not_create_distance_or_hr(tmp_path):
    sess = fixture_session(tmp_path, 40)
    sess.gps = sess.gps[:1]
    sess.heart_rate = sess.heart_rate[:1]
    s = analyze(sess)['summary']
    assert s['distance_km'] == pytest.approx(.02)
    assert s['gps_unknown_s'] == 30
    assert s['hr_coverage'] == .125


def test_empty_and_safe_html(tmp_path):
    sess = fixture_session(tmp_path)
    sess.gps, sess.heart_rate = None, []
    result = analyze(sess)
    assert result['summary']['mean_hr_bpm'] is None
    assert result['summary']['gps_coverage'] == 0
    output = tmp_path/'report.html'
    write_report(sess, output, title='</script><script>alert(1)</script>')
    html = output.read_text()
    assert '</script><script>alert(1)' not in html
    assert '__CYCLING_DATA__' not in html
    assert '\\u003c/script' in html
    assert 'https://' not in html
    json.dumps(result, allow_nan=False)


def test_position_quality_is_reported_as_unknown_speed(tmp_path):
    sess = fixture_session(tmp_path)
    sess.gps['h_acc_m'] = 100
    result = analyze(sess)
    assert result['summary']['distance_km'] == 0
    assert result['summary']['gps_unknown_s'] == 20
    assert all(p['lat'] is None for p in result['timeline'])


def test_cadence_is_experimental_and_missing_windows_stay_blank(tmp_path, monkeypatch):
    monkeypatch.setattr('bladetag.cycling.cadence_track', lambda sess: [
        dict(t_s=2., rpm=60., speed_kmh=18.),
        dict(t_s=8., rpm=None, speed_kmh=18.),
        dict(t_s=17., rpm=80., speed_kmh=24.)])
    result = analyze(fixture_session(tmp_path))
    assert result['summary']['cadence_rpm'] == 70
    assert result['summary']['development_m'] == 5
    assert [p['cadence'] for p in result['timeline']] == [60, None, None, 80]
    assert 'experimental' in ' '.join(result['findings'])


def test_behavior_blocks_exclude_gaps_stops_and_sparse_cadence():
    from bladetag.cycling_correlations import behavior_correlations
    rows = [dict(t=t, speed=20+t/100, hr=100+t/20, cadence=60+t/200)
            for t in range(0, 900, 5)]
    result = behavior_correlations(rows, [dict(start_s=300, end_s=450, duration_s=150)])
    assert len(result['blocks']) == 30
    assert result['correlations'][0]['r'] == pytest.approx(1)
    assert result['correlations'][3]['n'] == 10
    assert result['correlations'][6]['n'] == 15
    for r in rows[:2]:
        r['hr'] = None
    for r in rows[6:10]:
        r['cadence'] = None
    for r in rows[12:14]:
        r['speed'] = 0
    result = behavior_correlations(rows, [])
    assert result['blocks'][0]['t'] == 30
    assert result['blocks'][0]['cadence'] is None
    assert all(b['t'] != 60 for b in result['blocks'])
    constant = [dict(t=t, speed=20, hr=100, cadence=None) for t in range(0, 600, 5)]
    assert all(r['r'] is None for r in behavior_correlations(constant, [])['correlations'])
