"""Photos for the ride map: capture time, GPS and a small embedded thumbnail.

Phone cameras write the capture time as local wall-clock time, often without
a time zone. The zone is taken from OffsetTimeOriginal when present; otherwise
the offset (in 15-minute steps) that best puts the photos' own GPS positions
on the recorded track, falling back to this computer's zone. The page places
a photo on the track by its time, so tunnels and a stale camera fix don't
matter; the photo's own GPS is only used when no ride was recording.

Full-size images aren't copied: the page links to them relative to itself.
"""
from __future__ import annotations

import base64
import io
import os
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

EXTENSIONS = {'.jpg', '.jpeg', '.png', '.webp'}
THUMB_PX = 320
MATCH_M = 300.0      # median photo-to-track distance that confirms a time zone


def collect(paths) -> list[Path]:
    files = []
    for p in map(Path, paths):
        if p.is_dir():
            files += sorted(f for f in p.iterdir() if f.suffix.lower() in EXTENSIONS and not f.name.startswith('.'))
        elif p.suffix.lower() in EXTENSIONS:
            files.append(p)
    return files


def _ratio(x):
    try:
        return float(x)
    except (TypeError, ZeroDivisionError, ValueError):
        return float(x[0]) / float(x[1]) if isinstance(x, tuple) and x[1] else float('nan')


def _dms(v, ref):
    if not v:
        return None
    deg = _ratio(v[0]) + _ratio(v[1]) / 60 + _ratio(v[2]) / 3600
    return -deg if ref in ('S', 'W') else deg


def read(path: Path) -> dict | None:
    """Local capture time, optional offset and GPS, and a JPEG thumbnail data URI."""
    from PIL import Image, ImageOps
    try:
        with Image.open(path) as im:
            exif = im.getexif()
            sub = exif.get_ifd(0x8769)
            gps = exif.get_ifd(0x8825)
            stamp = sub.get(36867) or exif.get(306)            # DateTimeOriginal, DateTime
            offset = sub.get(36881) or sub.get(36880)          # OffsetTimeOriginal, OffsetTime
            im.draft('RGB', (THUMB_PX * 2, THUMB_PX * 2))
            thumb = ImageOps.exif_transpose(im).convert('RGB')
            thumb.thumbnail((THUMB_PX, THUMB_PX))
            buf = io.BytesIO()
            thumb.save(buf, 'JPEG', quality=68, optimize=True)
    except (OSError, ValueError):
        return None
    if not stamp:
        return None
    try:
        local = datetime.strptime(str(stamp).strip('\x00 '), '%Y:%m:%d %H:%M:%S')
    except ValueError:
        return None
    tz = None
    if offset:
        try:
            tz = datetime.strptime(str(offset).strip('\x00 '), '%z').tzinfo
        except ValueError:
            pass
    lat = _dms(gps.get(2), gps.get(1)) if gps else None
    lon = _dms(gps.get(4), gps.get(3)) if gps else None
    if lat is not None and (not np.isfinite(lat) or not np.isfinite(lon) or (lat == 0 and lon == 0)):
        lat = lon = None
    return dict(path=path, local=local, tz=tz, lat=lat, lon=lon,
                thumb='data:image/jpeg;base64,' + base64.b64encode(buf.getvalue()).decode(),
                w=thumb.width, h=thumb.height)


def _track(rides):
    """All ride positions as (utc seconds, lat, lon)."""
    ts, la, lo = [], [], []
    for r in rides:
        t0 = datetime.fromisoformat(r['start_utc'].replace('Z', '+00:00')).timestamp()
        p = r['points']
        for t, a, o in zip(p['t'], p['lat'], p['lon']):
            if a is not None and o is not None:
                ts.append(t0 + t); la.append(a); lo.append(o)
    order = np.argsort(ts)
    return np.array(ts)[order], np.array(la)[order], np.array(lo)[order]


def _miss(photos, track, offset_s):
    """Median metres between photos' GPS and the track at their capture time, if enough overlap."""
    ts, la, lo = track
    d = []
    for p in photos:
        if p['lat'] is None:
            continue
        u = p['local'].replace(tzinfo=timezone.utc).timestamp() - offset_s
        i = np.searchsorted(ts, u)
        if i == 0 or i == len(ts) or ts[i] - ts[i - 1] > 60:
            continue
        k = i if abs(ts[i] - u) < abs(ts[i - 1] - u) else i - 1
        kx = np.cos(np.radians(la[k])) * 111_320
        d.append(np.hypot((p['lat'] - la[k]) * 111_320, (p['lon'] - lo[k]) * kx))
    return (float(np.median(d)), len(d)) if len(d) >= 3 else (None, len(d))


def place(paths, rides, output: Path) -> tuple[list[dict], str]:
    """Photos for the page (sorted by time) and a sentence on how the time zone was chosen."""
    photos = [p for p in (read(f) for f in collect(paths)) if p]
    if not photos:
        return [], ''
    local_s = datetime.now().astimezone().utcoffset().total_seconds()
    need = [p for p in photos if p['tz'] is None]
    note = ''
    if need:
        track = _track(rides)
        miss, n = _miss(need, track, local_s) if len(track[0]) else (None, 0)
        best = local_s
        if miss is None or miss > MATCH_M:
            trials = [(m, off) for off in np.arange(-12 * 3600, 14 * 3600 + 1, 900)
                      for m, k in [_miss(need, track, off)] if m is not None]
            if trials and min(trials)[0] <= MATCH_M:
                miss, best = min(trials)
        hours = f'UTC{"+" if best >= 0 else "−"}{int(abs(best) // 3600):02d}:{int(abs(best) % 3600 // 60):02d}'
        note = (f'{len(need)} photos have no time zone; {hours} puts their GPS positions a median {miss:.0f} m from the track.'
                if miss is not None and miss <= MATCH_M else
                f'{len(need)} photos have no time zone and too few match the track; assumed {hours} (this computer).')
        for p in need:
            p['tz'] = timezone(timedelta(seconds=float(best)))
    out = []
    for p in photos:
        utc = p['local'].replace(tzinfo=p['tz']).astimezone(timezone.utc)
        rel = os.path.relpath(p['path'].resolve(), Path(output).resolve().parent)
        out.append(dict(utc=int(utc.timestamp() * 1000), name=p['path'].name,
                        src=urllib.parse.quote(Path(rel).as_posix()), thumb=p['thumb'], w=p['w'], h=p['h'],
                        lat=None if p['lat'] is None else round(p['lat'], 6),
                        lon=None if p['lon'] is None else round(p['lon'], 6)))
    out.sort(key=lambda p: p['utc'])
    return out, note
