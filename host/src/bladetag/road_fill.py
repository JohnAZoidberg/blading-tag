"""Fill phone-GPS blackouts (tunnels) along OpenStreetMap roads.

A tunnel leaves a gap in the GPS track: no position, no speed, so no distance.
For each gap between two stable stretches of good fixes, this finds the road
path between them in OpenStreetMap and inserts synthetic 1 Hz fixes along it
at constant speed (path length / gap time). Altitude is left unknown; the
ride map interpolates it between the portals.

Only the bounding boxes around the gaps go to the Overpass server, in one
request per ride, and the answer is cached on disk. Any failure falls back to
a speed-interpolated distance on a straight line, marked as not road-matched.
"""
from __future__ import annotations

import dataclasses
import hashlib
import heapq
import json
import math
import os
import urllib.parse
import urllib.request
from pathlib import Path

import numpy as np

from .session import Session

OVERPASS = 'https://overpass-api.de/api/interpreter'
USER_AGENT = 'bladetag-ride-map/1 (+https://wiki.openstreetmap.org/wiki/Overpass_API)'
ANCHOR_ACC_M = 15.0    # fixes trusted as gap anchors
ANCHOR_RUN = 3         # ...in runs of at least this many, ≤2 s apart
MIN_GAP_S = 10.0       # shorter blackouts are left to the usual 10 s hold
STOP_M = 50.0          # anchors closer than this: a stop, not a journey
SNAP_M = 60.0          # anchor must be this close to a road
BOX_PAD_M = 400.0
PORTAL_FIXES = 10      # fixes after/before a gap whose altitude isn't trusted
ROADS = {'motorway', 'trunk', 'primary', 'secondary', 'tertiary', 'unclassified', 'residential',
         'service', 'living_street', 'road', 'track', 'cycleway', 'busway',
         'motorway_link', 'trunk_link', 'primary_link', 'secondary_link', 'tertiary_link'}
NO_BIKE = {'motorway', 'motorway_link'}


def _hav(a1, o1, a2, o2):
    p1, p2 = np.radians(a1), np.radians(a2)
    dp, dl = p2 - p1, np.radians(np.asarray(o2) - np.asarray(o1))
    return 2 * 6371000 * np.arcsin(np.sqrt(np.sin(dp / 2) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(dl / 2) ** 2))


def bike_allowed(tags: dict) -> bool:
    if tags.get('bicycle') in ('yes', 'designated', 'permissive'):
        return True
    if tags.get('bicycle') in ('no', 'dismount', 'use_sidepath') or tags.get('motorroad') == 'yes':
        return False
    if tags.get('access') in ('no', 'private') or tags.get('highway') in NO_BIKE:
        return False
    return True


def find_gaps(gps: np.ndarray, t: np.ndarray) -> list[dict]:
    """Blackouts between stable runs of good fixes (isolated poor fixes don't split one)."""
    good = (np.isfinite(gps['lat']) & np.isfinite(gps['lon']) & np.isfinite(gps['h_acc_m'])
            & (gps['h_acc_m'] >= 0) & (gps['h_acc_m'] <= ANCHOR_ACC_M))
    idx = np.flatnonzero(good)
    if len(idx) < 2:
        return []
    runs = np.split(idx, np.flatnonzero(np.diff(t[idx]) > 2) + 1)
    runs = [r for r in runs if len(r) >= ANCHOR_RUN]
    gaps = []
    for prev, nxt in zip(runs, runs[1:]):
        a, b = prev[-1], nxt[0]
        if t[b] - t[a] >= MIN_GAP_S:
            gaps.append(dict(a=int(a), b=int(b), t0=float(t[a]), t1=float(t[b]),
                             before=prev[-PORTAL_FIXES * 2:-PORTAL_FIXES] if len(prev) > PORTAL_FIXES else prev[:1],
                             after=nxt[PORTAL_FIXES:PORTAL_FIXES * 2] if len(nxt) > PORTAL_FIXES else nxt[-1:]))
    return gaps


def _bbox(lat, lon, pad_m=BOX_PAD_M):
    dlat = pad_m / 111_320
    dlon = pad_m / (111_320 * max(.2, math.cos(math.radians(lat[0]))))
    return (min(lat) - dlat, min(lon) - dlon, max(lat) + dlat, max(lon) + dlon)


def _cache_dir() -> Path:
    return Path(os.environ.get('XDG_CACHE_HOME') or Path.home() / '.cache') / 'bladetag' / 'osm'


def overpass_roads(boxes: list[tuple]) -> list[dict]:
    """Road ways with node ids and geometry inside the boxes (cached on disk)."""
    parts = ''.join(f'way["highway"]({s:.5f},{w:.5f},{n:.5f},{e:.5f});' for s, w, n, e in boxes)
    query = f'[out:json][timeout:90];({parts});out body geom;'
    cache = _cache_dir() / (hashlib.sha256(query.encode()).hexdigest()[:24] + '.json')
    if cache.exists():
        return json.loads(cache.read_text())
    req = urllib.request.Request(OVERPASS, data=urllib.parse.urlencode({'data': query}).encode(),
                                 headers={'User-Agent': USER_AGENT})
    with urllib.request.urlopen(req, timeout=120) as resp:
        ways = [e for e in json.load(resp).get('elements', []) if e.get('type') == 'way' and e.get('geometry')]
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(ways))
    return ways


class RoadGraph:
    def __init__(self, ways: list[dict], bikes_only: bool):
        self.pos: dict = {}
        self.adj: dict = {}
        self.segs = []      # (u, v, way)
        for w in ways:
            tags = w.get('tags', {})
            if tags.get('highway') not in ROADS or (bikes_only and not bike_allowed(tags)):
                continue
            ids, geom = w.get('nodes') or [], w['geometry']
            if len(ids) != len(geom):
                ids = [f"{w['id']}:{i}" for i in range(len(geom))]
            oneway = tags.get('oneway') == 'yes' and tags.get('oneway:bicycle') != 'no'
            for (u, gu), (v, gv) in zip(zip(ids, geom), zip(ids[1:], geom[1:])):
                self.pos[u], self.pos[v] = (gu['lat'], gu['lon']), (gv['lat'], gv['lon'])
                d = float(_hav(gu['lat'], gu['lon'], gv['lat'], gv['lon']))
                self.adj.setdefault(u, []).append((v, d, w))
                if not oneway:
                    self.adj.setdefault(v, []).append((u, d, w))
                self.segs.append((u, v, w))

    def snap(self, lat, lon):
        """Nearest point on any segment: (distance m, u, v, fraction along u→v)."""
        best = None
        kx = math.cos(math.radians(lat)) * 111_320
        for u, v, w in self.segs:
            (a1, o1), (a2, o2) = self.pos[u], self.pos[v]
            ax, ay = (o1 - lon) * kx, (a1 - lat) * 111_320
            bx, by = (o2 - lon) * kx, (a2 - lat) * 111_320
            dx, dy = bx - ax, by - ay
            L2 = dx * dx + dy * dy
            f = 0. if L2 == 0 else min(1., max(0., -(ax * dx + ay * dy) / L2))
            dist = math.hypot(ax + f * dx, ay + f * dy)
            if best is None or dist < best[0]:
                best = (dist, u, v, f, w)
        return best

    def route(self, a, b):
        """Shortest road path between two (lat, lon) points: (length m, [(lat, lon)], [(way, m)])."""
        sa, sb = self.snap(*a), self.snap(*b)
        if not sa or not sb or sa[0] > SNAP_M or sb[0] > SNAP_M:
            return None

        def point(s):
            (a1, o1), (a2, o2) = self.pos[s[1]], self.pos[s[2]]
            return (a1 + s[3] * (a2 - a1), o1 + s[3] * (o2 - o1))
        pa, pb = point(sa), point(sb)
        if (sa[1], sa[2]) == (sb[1], sb[2]) or (sa[1], sa[2]) == (sb[2], sb[1]):
            L = float(_hav(pa[0], pa[1], pb[0], pb[1]))
            return L, [pa, pb], [(sa[4], L)]
        # Virtual start node connects to both ends of its segment (respecting nothing fancier).
        start, goal = ('start',), ('goal',)
        extra = {start: []}
        for node, frac in ((sa[1], sa[3]), (sa[2], 1 - sa[3])):
            seg = float(_hav(*self.pos[sa[1]], *self.pos[sa[2]]))
            extra[start].append((node, seg * frac, sa[4]))
        ends = {}
        for node, frac in ((sb[1], sb[3]), (sb[2], 1 - sb[3])):
            seg = float(_hav(*self.pos[sb[1]], *self.pos[sb[2]]))
            ends[node] = (seg * frac, sb[4])
        dist, prev, heap = {start: 0.}, {}, [(0., 0, start)]
        n = 0
        while heap:
            d, _, u = heapq.heappop(heap)
            if u == goal:
                break
            if d > dist.get(u, math.inf):
                continue
            edges = extra.get(u) or self.adj.get(u, [])
            if u in ends:
                edges = [*edges, (goal, *ends[u])]
            for v, w_len, way in edges:
                nd = d + w_len
                if nd < dist.get(v, math.inf):
                    dist[v], prev[v] = nd, (u, way, w_len)
                    n += 1
                    heapq.heappush(heap, (nd, n, v))
        if goal not in dist:
            return None
        coords, used, node = [pb], [], goal
        while node != start:
            u, way, w_len = prev[node]
            used.append((way, w_len))
            coords.append(pa if u == start else self.pos[u])
            node = u
        return dist[goal], coords[::-1], used[::-1]


def _describe(used):
    total = sum(m for _, m in used) or 1.
    tunnel = sum(m for w, m in used if w.get('tags', {}).get('tunnel') not in (None, 'no'))
    names: dict = {}
    for w, m in used:
        tg = w.get('tags', {})
        label = ' '.join(x for x in (tg.get('ref'), tg.get('name:en') or tg.get('name')) if x) or tg.get('highway', '')
        names[label] = names.get(label, 0.) + m
    return tunnel / total, max(names, key=names.get) if names else '', all(bike_allowed(w.get('tags', {})) for w, _ in used)


def fill_gaps(sess: Session, roads=None) -> tuple[Session, list[dict]]:
    """Return a session copy with gaps filled, and one record per gap.

    ``roads`` is a callable taking bounding boxes and returning OSM ways, or
    False to skip road matching (straight line, speed-interpolated distance).
    """
    gps = sess.gps
    if gps is None or not len(gps):
        return sess, []
    order = np.argsort(gps['elapsed_ns'], kind='stable')
    gps = gps[order]
    t = (gps['elapsed_ns'] - sess.start_elapsed_ns) / 1e9
    gaps = find_gaps(gps, t)
    if not gaps:
        return sess, []
    if roads is None:
        roads = overpass_roads
    journeys = [g for g in gaps if _hav(gps['lat'][g['a']], gps['lon'][g['a']], gps['lat'][g['b']], gps['lon'][g['b']]) >= STOP_M]
    ways, error = [], ''
    if roads and journeys:
        boxes = [_bbox([gps['lat'][g['a']], gps['lat'][g['b']]], [gps['lon'][g['a']], gps['lon'][g['b']]]) for g in journeys]
        try:
            ways = roads(boxes)
        except Exception as exc:     # offline, rate limited, …: fall back, never fail the report
            error = f'{type(exc).__name__}: {exc}'
    graphs = [RoadGraph(ways, True), RoadGraph(ways, False)] if ways else []
    records, inserts, drop = [], [], np.zeros(len(gps), bool)
    for g in gaps:
        a, b = g['a'], g['b']
        A, B = (gps['lat'][a], gps['lon'][a]), (gps['lat'][b], gps['lon'][b])
        dt = g['t1'] - g['t0']
        straight = float(_hav(*A, *B))
        near = [s for s in (gps['speed_mps'][max(0, a - 5):a + 1], gps['speed_mps'][b:b + 6]) if np.isfinite(s).any()]
        around = float(np.mean([np.nanmedian(s) for s in near])) if near else np.nan
        rec = dict(t0_s=g['t0'], t1_s=g['t1'], duration_s=dt, straight_m=straight, speed_around_kmh=around * 3.6,
                   method='', length_m=None, road='', tunnel_share=None, bikes_allowed=None, note=error)
        coords = [A, B]
        if straight < STOP_M:
            rec.update(method='stop', length_m=straight)
        else:
            options = []
            for k, graph in enumerate(graphs):
                r = graph.route(A, B)
                if r and straight * .98 <= r[0] <= max(3 * straight, straight + 1500):
                    implied = r[0] / dt
                    fit = abs(math.log(max(implied, .5) / max(around, .5))) if np.isfinite(around) else 0.
                    options.append((fit + .15 * k, r))   # prefer bike-legal roads unless speed says otherwise
            if options:
                L, coords, used = min(options, key=lambda o: o[0])[1]
                share, road, legal = _describe(used)
                rec.update(method='road', length_m=L, road=road, tunnel_share=share, bikes_allowed=legal)
            else:
                guess = around * dt if np.isfinite(around) else straight
                rec.update(method='speed', length_m=max(straight, guess),
                           note=error or ('no road path found' if graphs else 'road matching off'))
        rec['speed_kmh'] = rec['length_m'] / dt * 3.6 if dt else None
        records.append(rec)
        drop[a + 1:b] = True        # poor or missing fixes inside the blackout
        # Synthetic fixes at 1 Hz, evenly spaced along the path.
        cl = np.array(coords, float)
        seg = _hav(cl[:-1, 0], cl[:-1, 1], cl[1:, 0], cl[1:, 1]) if len(cl) > 1 else np.zeros(0)
        cum = np.r_[0., np.cumsum(seg)]
        ts = np.arange(g['t0'] + 1, g['t1'] - .5, 1.)
        if not len(ts):
            continue
        frac = (ts - g['t0']) / dt
        target = frac * cum[-1]
        fix = np.full(len(ts), np.nan, dtype=gps.dtype)
        fix['elapsed_ns'] = sess.start_elapsed_ns + ts * 1e9
        if cum[-1] > 0:
            fix['lat'] = np.interp(target, cum, cl[:, 0])
            fix['lon'] = np.interp(target, cum, cl[:, 1])
        else:
            fix['lat'], fix['lon'] = A
        fix['speed_mps'] = rec['length_m'] / dt
        fix['h_acc_m'], fix['speed_acc_mps'] = 5., 1.
        if 'utc_ms' in gps.dtype.names:
            fix['utc_ms'] = np.interp(ts, t[[a, b]], gps['utc_ms'][[a, b]]) if np.isfinite(gps['utc_ms'][[a, b]]).all() else np.nan
        inserts.append(fix)
        rec['portal_alt_m'] = (float(np.nanmedian(gps['alt_m'][g['before']])) if np.isfinite(gps['alt_m'][g['before']]).any() else None,
                               float(np.nanmedian(gps['alt_m'][g['after']])) if np.isfinite(gps['alt_m'][g['after']]).any() else None)
    filled = np.concatenate([gps[~drop], *inserts])
    filled = filled[np.argsort(filled['elapsed_ns'], kind='stable')]
    return dataclasses.replace(sess, gps=filled), records
