"""Periodic, local foot-tag motion for cycling replay.

Translation is reconstructed from acceleration; it is never forced onto a
crank circle. Unknown constant velocity and acceleration bias are removed by
assuming each accepted pedal turn repeats. Heading and shoe mount are not
observable, so each turn is aligned to its horizontal principal axis and the
shoe's neutral attitude is illustrative.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
from scipy import signal
from scipy.spatial.transform import Rotation

from .cycling_signals import cadence_track
from .session import Session, load_session
from .timesync import fit_clocks
from .viz3d import G, Params, _mahony, _level


def periodic_path(acceleration: np.ndarray, period_s: float) -> np.ndarray:
    """Zero-mean periodic displacement, metres, from evenly spaced m/s².

    The zero-frequency acceleration and velocity cannot be recovered from
    an IMU loop. Fourier integration removes those constants and suppresses
    harmonics above six cycles per revolution (road vibration).
    """
    n = len(acceleration)
    if n < 8 or period_s <= 0:
        raise ValueError('a positive period and at least eight samples are required')
    spectrum = np.fft.rfft(acceleration, axis=0)
    k = np.arange(len(spectrum))
    omega = 2*np.pi*k / period_s
    divisor = np.where(k > 0, -(omega**2), np.inf)
    spectrum = spectrum / divisor[:, None]
    spectrum[k > 6] = 0
    return np.fft.irfft(spectrum, n, axis=0)


def _metrics(pos, quat):
    span = np.ptp(pos, axis=0)
    rv = Rotation.from_quat(quat).as_rotvec()
    return dict(fore_aft_cm=float(span[0]*100), lateral_cm=float(span[1]*100),
                vertical_cm=float(span[2]*100),
                relative_tilt_deg=float(np.degrees(np.max(np.linalg.norm(rv, axis=1)))))


def _cycle(acc, gyr, fs, center, expected_period, frames):
    """One complete measured turn near the centre of an eight-second window."""
    # Initialise tilt from several pedal turns, not a phase-biased first second.
    pad = int(2*fs)
    padded_acc = np.r_[np.repeat(acc.mean(axis=0)[None,:],pad,axis=0),acc]
    padded_gyr = np.r_[np.zeros((pad,3)),gyr]
    alp = signal.sosfiltfilt(signal.butter(2,.4,fs=fs,output='sos'),padded_acc,axis=0)
    rw = _mahony(alp,np.radians(padded_gyr),1/fs,
                  Params(kp=.1,ki=0.))[pad:]
    # A single window-wide gravity correction avoids treating pedal-frequency
    # acceleration as changing tilt. Constant horizontal acceleration is unknown.
    gravity = np.einsum('nij,nj->ni',rw,acc).mean(axis=0)
    rw = _level(gravity) @ rw
    aw = np.einsum('nij,nj->ni', rw, acc)*G
    vertical = signal.sosfiltfilt(signal.butter(2, 4., fs=fs, output='sos'), aw[:, 2])
    peaks, _ = signal.find_peaks(vertical, distance=max(1, int(.65*expected_period*fs)))
    pairs = [(a, b) for a,b in zip(peaks[:-1], peaks[1:])
             if .8*expected_period <= (b-a)/fs <= 1.2*expected_period]
    if not pairs:
        return None
    a,b = min(pairs, key=lambda ab: abs((ab[0]+ab[1])/2-center*fs))
    period = (b-a)/fs
    u = np.linspace(a, b, frames, endpoint=False)
    acceleration = np.column_stack([np.interp(u, np.arange(len(acc)), aw[:, k]) for k in range(3)])
    pos = periodic_path(acceleration, period)
    # Per-turn yaw is unobservable: horizontal long axis defines the displayed x.
    _, _, vt = np.linalg.svd(pos[:, :2], full_matrices=False)
    forward = vt[0]
    top = int(np.argmax(pos[:,2]))
    if np.dot(pos[(top+1)%frames,:2]-pos[(top-1)%frames,:2], forward) < 0:
        forward = -forward
    basis = np.array([[forward[0],forward[1],0],[-forward[1],forward[0],0],[0,0,1.]])
    pos = pos @ basis.T
    matrices = rw[np.clip(np.rint(u).astype(int),0,len(rw)-1)]
    neutral = Rotation.from_matrix(matrices).mean().as_matrix()
    relative = basis @ matrices @ neutral.T @ basis.T
    quat = Rotation.from_matrix(relative).as_quat()
    # All loops start with the highest measured tag position; no crank-angle claim.
    pos, quat = np.roll(pos,-top,axis=0), np.roll(quat,-top,axis=0)
    pos, quat = np.r_[pos,pos[:1]], np.r_[quat,quat[:1]]
    return dict(period_s=float(period),rpm=float(60/period),
                pos=pos,quat=quat,metrics=_metrics(pos,quat),offset_s=float(a/fs + top*period/frames))


def build(path: str | Path | Session, *, max_cycles: int = 600, frames: int = 64) -> dict:
    """Return sampled reliable pedal turns and phase-normalised typical loops."""
    if max_cycles < 1 or frames < 16:
        raise ValueError('max_cycles must be positive and frames at least 16')
    sess = path if isinstance(path, Session) else load_session(path)
    tag = next((t for t in sess.tags if t.meta.position in ('left_foot','right_foot')), None)
    if tag is None:
        raise ValueError('Cycling replay needs a tag labelled left_foot or right_foot')
    accepted = [r for r in cadence_track(sess) if r['rpm'] is not None]
    if not accepted:
        raise ValueError('No reliable moving pedal windows were found for 3D replay')
    # Even sampling retains coverage of the outing without making a huge web page.
    chosen = np.unique(np.linspace(0,len(accepted)-1,min(max_cycles,len(accepted))).astype(int))
    clock = fit_clocks(tag)
    t = (clock.sample_elapsed_ns(tag.sample_index)-sess.start_elapsed_ns)/1e9
    acc, gyr = tag.accel_g, tag.gyro_dps
    stride = max(1,int(clock.rate_hz/50))
    fs = clock.rate_hz/stride
    cycles = []
    hrs = sorted(sess.heart_rate,key=lambda r:r['elapsed_ns'])
    ht = np.array([(r['elapsed_ns']-sess.start_elapsed_ns)/1e9 for r in hrs])
    for i in chosen:
        row = accepted[i]
        a,b = np.searchsorted(t,[row['t_s']-4,row['t_s']+4])
        if b-a < clock.rate_hz*7.8 or np.any(np.diff(tag.sample_index[a:b]) != 1):
            continue
        # Anti-alias before decimating the IMU to approximately 50 Hz.
        sos = signal.butter(2,10,fs=clock.rate_hz,output='sos')
        ac = signal.sosfiltfilt(sos,acc[a:b],axis=0)[::stride]
        gy = signal.sosfiltfilt(sos,gyr[a:b],axis=0)[::stride]
        cycle = _cycle(ac,gy,fs,4.,60/row['rpm'],frames)
        if cycle is None:
            continue
        # Reject gross integration failures, not flatten or resize their paths.
        span = np.ptp(cycle['pos'],axis=0)
        if not np.isfinite(span).all() or max(span) > 1.2 or max(span) < .05:
            continue
        cycle['t_s'] = float(t[a]+cycle.pop('offset_s'))
        cycle['speed_kmh'] = row['speed_kmh']
        cycle['strength'] = row['strength']
        j = np.searchsorted(ht,cycle['t_s'],side='right')-1
        hr = hrs[j] if j >= 0 and cycle['t_s']-ht[j] < 5 else {}
        bpm = hr.get('bpm')
        cycle['hr_bpm'] = float(bpm) if bpm and 25<=bpm<=240 and hr.get('contact_detected') is not False else None
        cycle['pos'] = np.round(cycle['pos'],5).tolist()
        cycle['quat'] = np.round(cycle['quat'],6).tolist()
        cycles.append(cycle)
    if not cycles:
        raise ValueError('No complete pedal turns passed the motion reconstruction checks')
    def typical(part):
        positions = np.array([c['pos'] for c in part])
        p = np.median(positions,axis=0)
        q = np.array(part[len(part)//2]['quat'])
        return dict(pos=p.round(5).tolist(),quat=q.tolist(),metrics=_metrics(p,q),count=len(part),rpm=float(np.median([c['rpm'] for c in part])))
    k = max(1,len(cycles)//5)
    end = sess.meta.get('end_elapsed_ns')
    duration = (end-sess.start_elapsed_ns)/1e9 if end else t[-1]
    return dict(session=sess.path.name,start_utc=sess.meta['start_utc'],gear=sess.gear,
                side=tag.meta.position,duration_s=float(duration),cycles=cycles,
                typical=dict(all=typical(cycles),early=typical(cycles[:k]),late=typical(cycles[-k:])),
                summary=dict(cycles=len(cycles),accepted_windows=len(accepted),
                             cadence_rpm=float(np.median([c['rpm'] for c in cycles])),
                             **{key:float(np.median([c['metrics'][key] for c in cycles])) for key in cycles[0]['metrics']}),
                methods=[
                    'Sampled reliable 8-second foot-IMU windows; one measured complete turn per selected window. This is not every pedal turn, and gaps are not coasting.',
                    'Gyro plus low-passed acceleration estimate local orientation; mean world acceleration is levelled over the 8-second window. No magnetometer or crank sensor is available.',
                    'Acceleration is integrated twice with periodic boundary conditions. Constant acceleration/velocity and harmonics above six per turn are removed. Paths are not forced onto a circle and are not crank-radius measurements.',
                    'Every loop is centred and starts at its highest tag position. Horizontal PCA sets the displayed fore-aft direction; its sign is conventional. Absolute heading, toe angle and shoe mount are not measured.',
                    'The shoe model has an illustrative neutral mount. Its relative rotation comes from the IMU, but absolute shoe pose, crank angle, opposite foot and bike geometry are schematic.',
                    'Early and late are the first/last 20% of sampled accepted turns. Typical paths are phase-wise medians; their rotation uses a representative measured turn, not an averaged anatomical pose.',
                ])


def write_report(path, output, title='Cycling pedal replay'):
    data = build(path)
    data['title'] = title
    payload = json.dumps(data,allow_nan=False,separators=(',',':')).replace('<','\\u003c').replace('>','\\u003e').replace('&','\\u0026')
    template = Path(__file__).with_name('cycling3d_template.html').read_text()
    output = Path(output)
    output.write_text(template.replace('__CYCLING3D_DATA__',payload))
    return data
