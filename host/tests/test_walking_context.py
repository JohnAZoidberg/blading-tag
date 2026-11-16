import json

import numpy as np
import pytest

from bladetag.session import GPS_COLUMNS, Session
from bladetag.walking_context import analyze_session


def make_session(tmp_path, seconds=20):
    origin = 1_000_000_000
    gps = np.full(3, np.nan, dtype=[(c, 'f8') for c in GPS_COLUMNS])
    gps['elapsed_ns'] = origin + np.array([.2, 5.5, 15.25]) * 1e9
    gps['speed_mps'] = [1, 0, 2]
    gps['lat'], gps['lon'], gps['h_acc_m'] = 25, 121, 5
    hr = [dict(elapsed_ns=origin, bpm=100, contact_detected=True),
          dict(elapsed_ns=origin+10_000_000_000, bpm=120, contact_detected=False),
          dict(elapsed_ns=origin+15_000_000_000, bpm=120, contact_detected=True)]
    return Session(tmp_path, dict(start_elapsed_ns=origin, end_elapsed_ns=origin+seconds*1e9), [], gps, heart_rate=hr)


def test_exact_intervals_and_contact(tmp_path):
    result = analyze_session(make_session(tmp_path))
    s = result['summary']
    assert s['distance_km'] == pytest.approx((5 + 4.75*2)/1000)
    assert s['gps_unknown_s'] == pytest.approx(5.25)
    assert s['mean_hr_bpm'] == pytest.approx(110)
    assert s['hr_coverage'] == .5
    assert s['moving_s'] == pytest.approx(9.75)
    assert result['timeline'][11]['hr_bpm'] is None
    json.dumps(result, allow_nan=False)


def test_gaps_bad_speed_and_invalid_position(tmp_path):
    sess = make_session(tmp_path, 60)
    sess.gps['h_acc_m'][1] = 50
    sess.gps['speed_acc_mps'][2] = 2
    result = analyze_session(sess)
    assert result['summary']['distance_km'] == pytest.approx(.005)
    assert result['summary']['gps_coverage'] == pytest.approx(5/60)
    assert result['timeline'][6]['lat'] is None
    assert result['timeline'][30]['speed_kmh'] is None
    assert not result['stops']


def test_no_sensors_and_missing_end(tmp_path):
    sess = make_session(tmp_path)
    sess.gps, sess.heart_rate = None, []
    result = analyze_session(sess)
    assert result['summary']['gps_unknown_s'] == 20
    assert result['summary']['mean_hr_bpm'] is None
    del sess.meta['end_elapsed_ns']
    result = analyze_session(sess)
    assert result['summary']['duration_s'] == 0
    assert result['quality']['duration_inferred']
    assert result['timeline'] == []


def test_prestart_and_after_end_excluded(tmp_path):
    sess = make_session(tmp_path)
    sess.gps['elapsed_ns'][0] = 0
    sess.gps['elapsed_ns'][2] = 50e9
    sess.heart_rate[0]['elapsed_ns'] = 0
    result = analyze_session(sess)
    assert result['summary']['distance_km'] == 0
    assert result['timeline'][0]['hr_bpm'] is None
    assert result['quality']['gps_fixes'] == 1


def test_unknown_breaks_low_speed_periods(tmp_path):
    sess = make_session(tmp_path, 40)
    sess.gps['elapsed_ns'] = sess.start_elapsed_ns + np.array([0, 4, 20])*1e9
    sess.gps['speed_mps'] = 0
    result = analyze_session(sess)
    assert result['stops'] == []
    assert result['summary']['stopped_s'] == 14


def test_pace_matched_hr_requires_both_halves(tmp_path):
    sess = make_session(tmp_path, 240)
    seconds = np.arange(240)
    gps = np.full(240, np.nan, dtype=sess.gps.dtype)
    gps['elapsed_ns'] = sess.start_elapsed_ns + seconds*1e9
    gps['speed_mps'], gps['h_acc_m'] = 1.5, 5
    sess.gps = gps
    sess.heart_rate = [dict(elapsed_ns=sess.start_elapsed_ns+int(t)*10**9,
                            bpm=100 if t < 120 else 110, contact_detected=True)
                       for t in seconds]
    result = analyze_session(sess)
    comparison, = result['pace_hr_comparison']
    assert comparison['change_bpm'] == 10
    assert comparison['early_s'] == 120
    assert comparison['late_s'] == 120
    sess.gps['speed_mps'][120:] = 1
    assert analyze_session(sess)['pace_hr_comparison'] == []


def test_missing_end_uses_observation_time_without_inventing_tail(tmp_path):
    sess = make_session(tmp_path)
    del sess.meta['end_elapsed_ns']
    result = analyze_session(sess)
    assert result['summary']['duration_s'] == 15.25
    assert result['quality']['duration_inferred']
    assert result['summary']['distance_km'] == pytest.approx(.005)
