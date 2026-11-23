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


def test_gap_without_roads_is_estimated_from_speed(tmp_path):
    r = analyze(ride(tmp_path, gap=(400, 500)), roads=False)
    (g,) = r['gaps']
    assert g['method'] == 'speed' and g['note'] == 'road matching off'
    assert g['length_m'] == pytest.approx(5 * (g['t1_s'] - g['t0_s']))
    s = r['summary']
    assert s['distance_km'] == pytest.approx(4.5, abs=.03)   # nothing lost in the gap
    assert s['filled_km'] == pytest.approx(g['length_m'] / 1000) and s['road_km'] == 0
    assert s['gps_coverage'] == pytest.approx(.9, abs=.02)    # real fixes only
    p = r['points']
    inside = [i for i, t in enumerate(p['t']) if 405 <= t < 495]
    assert all(p['fill'][i] == 2 for i in inside)
    assert all(p['alt'][i] is not None for i in inside)       # interpolated, profile unbroken
    # Default (online) mode falls back the same way when Overpass is unreachable.
    offline = analyze(ride(tmp_path, gap=(400, 500)))
    assert offline['gaps'][0]['method'] == 'speed' and 'network disabled' in offline['gaps'][0]['note']


def test_missing_altitude(tmp_path):
    none = analyze(ride(tmp_path, alt=lambda t: np.full(len(t), np.nan)), roads=False)
    assert none['summary']['alt_max_m'] is None
    assert none['climbs'] == []
    assert any('No usable GPS altitude' in f for f in none['findings'])


M_LAT = 1 / 111_320
M_LON = 1 / (111_320 * np.cos(np.radians(24.5)))


def l_ride(tmp_path):
    """North 1500 m, then east 1500 m, at 5 m/s; a 100 s tunnel around the corner."""
    t = np.arange(600, dtype=float)
    keep = (t < 250) | (t >= 350)
    t = t[keep]
    gps = np.full(len(t), np.nan, dtype=[(c, 'f8') for c in GPS_COLUMNS])
    gps['elapsed_ns'] = ORIGIN + t * 1e9
    gps['speed_mps'], gps['h_acc_m'], gps['speed_acc_mps'] = 5, 4, .5
    north = np.minimum(t, 300) * 5
    east = np.maximum(t - 300, 0) * 5
    gps['lat'] = 24.5 + north * M_LAT
    gps['lon'] = 121.8 + east * M_LON
    # 30 m climb up to the corner, then flat; portal fixes are 15 m off.
    gps['alt_m'] = np.minimum(north, 1500) / 50
    gps['alt_m'][(t >= 240) & (t < 250) | (t >= 350) & (t < 360)] += 15
    return Session(tmp_path, dict(start_elapsed_ns=ORIGIN, end_elapsed_ns=ORIGIN + 600e9,
                                  start_utc='2026-11-22T00:00:00Z', activity='cycling'), [], gps, heart_rate=[])


def way(i, tags, pts):
    return dict(type='way', id=i, tags=tags, nodes=[f'{i}-{k}' for k in range(len(pts))],
                geometry=[dict(lat=24.5 + n * M_LAT, lon=121.8 + e * M_LON) for n, e in pts])


def test_gap_follows_the_road_not_a_shortcut(tmp_path):
    corner = [(n, 0) for n in range(0, 1501, 100)] + [(1500, e) for e in range(100, 1501, 100)]
    roads = [way(1, dict(highway='primary', ref='9', name='Coast Road', tunnel='yes'), corner),
             # Shorter, but closed to bikes, and its implied speed wouldn't fit either.
             way(2, dict(highway='primary', bicycle='no'), [(1200, 0), (1500, 300)])]
    asked = []
    r = analyze(l_ride(tmp_path), roads=lambda boxes: asked.append(boxes) or roads)
    assert len(asked) == 1 and len(asked[0]) == 1
    (g,) = r['gaps']
    assert g['method'] == 'road' and g['road'] == '9 Coast Road' and g['bikes_allowed']
    assert g['tunnel_share'] == pytest.approx(1)
    assert g['length_m'] == pytest.approx(101 * 5, abs=5)      # along the road, not the 357 m diagonal
    assert r['summary']['distance_km'] == pytest.approx(3.0, abs=.03)
    assert r['summary']['tunnel_km'] == pytest.approx(.505, abs=.01)
    p = r['points']
    lat, lon = np.array(p['lat'], float), np.array(p['lon'], float)
    inside = [i for i, t in enumerate(p['t']) if 255 <= t < 345]
    # Every filled point lies on one of the two legs.
    off = np.minimum(np.abs(lon[inside] - 121.8) / M_LON, np.abs(lat[inside] - (24.5 + 1500 * M_LAT)) / M_LAT)
    assert off.max() < 15
    # Portal fixes' bad altitude is replaced by a ramp from ~29 m to the flat 30 m.
    alt = np.array([np.nan if a is None else a for a in p['alt']], float)
    near = [i for i, t in enumerate(p['t']) if 230 <= t < 370]
    assert np.nanmax(alt[near]) < 32


def test_page_is_safe_and_only_fetches_tiles(tmp_path):
    out = tmp_path / 'map.html'
    rides = write_report([ride(tmp_path, start='2026-11-22T06:00:00Z'), ride(tmp_path)], out,
                         title='</script><script>alert(1)</script>', roads=False)
    assert [r['start_utc'] for r in rides] == ['2026-11-22T00:00:00Z', '2026-11-22T06:00:00Z']
    html = out.read_text()
    assert '__RIDE_DATA__' not in html
    assert '</script><script>alert(1)' not in html
    payload = re.search(r'<script id="data" type="application/json">(.*?)</script>', html, re.S).group(1)
    data = json.loads(payload)
    assert data['title'] == '</script><script>alert(1)</script>' and len(data['rides']) == 3  # day + 2 rides
    hosts = set(re.findall(r'https://([^/\'"]+)', html))
    assert hosts <= {'server.arcgisonline.com', '{s}.tile.opentopomap.org'}, hosts


def test_day_combines_rides_end_to_end(tmp_path):
    from bladetag.ride_map import combine
    a = analyze(ride(tmp_path, start='2026-11-22T01:00:00Z'))
    b = analyze(ride(tmp_path, start='2026-11-22T03:00:00Z'))
    day = combine([b, a], '2026-11-22')
    s = day['summary']
    assert s['rides'] == 2 and day['members'] == [a['session'], b['session']]
    assert s['distance_km'] == pytest.approx(a['summary']['distance_km'] * 2)
    assert s['duration_s'] == pytest.approx(1800)
    assert s['span_s'] == pytest.approx(7200 + 900)
    # Same terrain per ride: totals and climbs add up; nothing spans the break.
    assert s['climb_m'] == pytest.approx(2 * a['summary']['climb_m'], abs=1)
    assert len(day['climbs']) == 2
    n = len(a['points']['t'])
    assert day['ride_starts'] == [0, n]
    assert day['points']['t'][n] == pytest.approx(7200)
    assert day['points']['d'][n] == pytest.approx(a['summary']['distance_km'] + .025, abs=.01)  # end of its first 5 s bin
    assert day['climbs'][1]['start_i'] >= n
    assert day['findings'][0].startswith('2 rides cover')


def test_page_lists_day_before_its_rides(tmp_path):
    out = tmp_path / 'map.html'
    write_report([ride(tmp_path, start='2026-11-22T03:00:00Z'), ride(tmp_path, start='2026-11-22T04:00:00Z')], out)
    data = json.loads(re.search(r'<script id="data" type="application/json">(.*?)</script>', out.read_text(), re.S).group(1))
    assert [bool(r.get('combined')) for r in data['rides']] == [True, False, False]
    write_report([ride(tmp_path)], out)
    data = json.loads(re.search(r'<script id="data" type="application/json">(.*?)</script>', out.read_text(), re.S).group(1))
    assert len(data['rides']) == 1


def test_rest_stops_merge_shuffles_and_skip_start_and_finish():
    from bladetag.ride_map import rest_stops
    t = np.arange(0, 3600, 5.)
    d = np.minimum(t, 3000) * 5.               # metres; standing still after 3000 s
    hr = np.full(len(t), 120.)
    alt = np.full(len(t), 50.)
    stops = [dict(start_s=0, end_s=400, duration_s=400),           # waiting at the start
             dict(start_s=1000, end_s=1180, duration_s=180),       # 3 min, then a 60 s shuffle…
             dict(start_s=1240, end_s=1400, duration_s=160),       # …then 160 s more: one 400 s rest
             dict(start_s=2000, end_s=2200, duration_s=200),       # too short
             dict(start_s=3000, end_s=3600, duration_s=600)]       # standing at the finish
    d[(t >= 1180) & (t < 1240)] = 1180 * 5     # the shuffle moves less than 150 m
    d[t >= 1240] -= 60 * 5
    (r,) = rest_stops(stops, t, d, hr, alt)
    assert (r['start_s'], r['end_s'], r['duration_s'], r['stopped_s']) == (1000, 1400, 400, 340)
    assert r['since_s'] == 1000 and r['km'] == pytest.approx(5.0)


def _photo(path, local, lat=None, lon=None):
    from PIL import Image
    im = Image.new('RGB', (800, 600), (40, 120, 200))
    exif = Image.Exif()
    exif.get_ifd(0x8769)[36867] = local
    if lat is not None:
        dms = lambda v: (float(int(v)), float(int(v * 60 % 60)), round(v * 3600 % 60, 4))
        gps = exif.get_ifd(0x8825)
        gps.update({1: 'N', 2: dms(lat), 3: 'E', 4: dms(lon)})
    im.save(path, exif=exif)


def test_photos_are_timed_by_the_track_and_linked_relative(tmp_path):
    pics = tmp_path / 'pictures'
    pics.mkdir()
    # Local wall-clock times without a zone; the ride runs 00:00–00:15 UTC = 08:00–08:15 in UTC+8.
    for k, s in enumerate((60, 300, 600)):
        _photo(pics / f'IMG_{k}.jpg', f'2026:11:22 08:{s // 60:02d}:00', 24.5 + s * 5 / 111_320, 121.8)
    _photo(pics / 'no_gps.jpg', '2026:11:22 07:50:00')
    (pics / 'notes.txt').write_text('not a photo')
    out = tmp_path / 'site' / 'map.html'
    out.parent.mkdir()
    write_report([ride(tmp_path)], out, roads=False, photos=[pics])
    data = json.loads(re.search(r'<script id="data" type="application/json">(.*?)</script>', out.read_text(), re.S).group(1))
    photos = data['photos']
    assert [p['name'] for p in photos] == ['no_gps.jpg', 'IMG_0.jpg', 'IMG_1.jpg', 'IMG_2.jpg']
    assert 'UTC+08:00' in data['photo_note'] and 'median' in data['photo_note']
    start = 1795305600000                       # 2026-11-22T00:00:00Z
    assert [(p['utc'] - start) // 1000 for p in photos] == [-600, 60, 300, 600]
    assert photos[1]['src'] == '../pictures/IMG_0.jpg'
    assert photos[1]['thumb'].startswith('data:image/jpeg;base64,') and max(photos[1]['w'], photos[1]['h']) <= 320
    assert photos[0]['lat'] is None
