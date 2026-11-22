import json
import re

import numpy as np
import pytest

from bladetag.ride_map import analyze, climb_descent, pivots, write_report
from bladetag.session import GPS_COLUMNS, Session

ORIGIN = 1_000_000_000_000


def ride(tmp_path, seconds=900, alt=None, start='2026-11-22T00:00:00Z', gap=None):
    """1 Hz GPS riding north at 5 m/s; altitude climbs 60 m over the first 300 s."""
    t = np.arange(seconds, dtype=float)
    if gap:
        t = t[(t < gap[0]) | (t >= gap[1])]
    gps = np.full(len(t), np.nan, dtype=[(c, 'f8') for c in GPS_COLUMNS])
    gps['elapsed_ns'] = ORIGIN + t * 1e9
    gps['speed_mps'], gps['h_acc_m'], gps['speed_acc_mps'] = 5, 4, .5
    gps['lat'] = 24.5 + t * 5 / 111_320
    gps['lon'] = 121.8
    rng = np.random.default_rng(1)
    gps['alt_m'] = (np.minimum(t, 300) * .2 + rng.normal(0, 1.5, len(t))) if alt is None else alt(t)
    hr = [dict(elapsed_ns=int(ORIGIN + s * 1e9), bpm=110 + (20 if s < 300 else 0), contact_detected=True)
          for s in range(0, seconds, 2)]
    return Session(tmp_path, dict(start_elapsed_ns=ORIGIN, end_elapsed_ns=ORIGIN + seconds * 1e9,
                                  start_utc=start, activity='cycling', gear='Roadbike'), [], gps, heart_rate=hr)


def test_climb_from_noisy_gps_altitude(tmp_path):
    r = analyze(ride(tmp_path))
    s = r['summary']
    # 60 m real gain; GPS noise must not inflate it much.
    assert s['climb_m'] == pytest.approx(60, abs=8)
    assert s['descent_m'] < 8
    assert len(r['climbs']) == 1
    c = r['climbs'][0]
    assert c['gain_m'] == pytest.approx(60, abs=8)
    assert c['grade_pct'] == pytest.approx(4, abs=.6)   # 60 m over 1.5 km
    assert c['hr_bpm'] == pytest.approx(130, abs=2)
    flat = next(b for b in r['grade_bands'] if b['lo'] == -1)
    assert flat['hr_bpm'] == pytest.approx(110, abs=2)
    assert any('climbing' in f for f in r['findings'])


def test_hysteresis_and_pivots():
    wiggle = np.tile([0., 2.], 50)
    assert climb_descent(wiggle) == (0, 0)
    up, down = climb_descent(np.r_[np.linspace(0, 40, 20), np.linspace(40, 10, 20)])
    assert up == pytest.approx(40, abs=3) and down == pytest.approx(30, abs=3)
    a = np.r_[np.linspace(0, 50, 30), np.linspace(50, 45, 5), np.linspace(45, 80, 20), np.linspace(80, 20, 30)]
    p = pivots(a)
    # The 5 m dip doesn't split the climb.
    assert [round(a[i]) for i in p] == [0, 80, 20]


def test_gap_breaks_profile_and_missing_altitude(tmp_path):
    r = analyze(ride(tmp_path, gap=(400, 500)))
    alt = r['points']['alt']
    t = r['points']['t']
    assert all(a is None for a, s in zip(alt, t) if 405 <= s < 495)
    none = analyze(ride(tmp_path, alt=lambda t: np.full(len(t), np.nan)))
    assert none['summary']['alt_max_m'] is None
    assert none['climbs'] == []
    assert any('No usable GPS altitude' in f for f in none['findings'])


def test_page_is_safe_and_only_fetches_tiles(tmp_path):
    out = tmp_path / 'map.html'
    rides = write_report([ride(tmp_path, start='2026-11-22T06:00:00Z'), ride(tmp_path)], out,
                         title='</script><script>alert(1)</script>')
    assert [r['start_utc'] for r in rides] == ['2026-11-22T00:00:00Z', '2026-11-22T06:00:00Z']
    html = out.read_text()
    assert '__RIDE_DATA__' not in html
    assert '</script><script>alert(1)' not in html
    payload = re.search(r'<script id="data" type="application/json">(.*?)</script>', html, re.S).group(1)
    data = json.loads(payload)
    assert data['title'] == '</script><script>alert(1)</script>' and len(data['rides']) == 2
    hosts = set(re.findall(r'https://([^/\'"]+)', html))
    assert hosts <= {'server.arcgisonline.com', '{s}.tile.opentopomap.org'}, hosts
