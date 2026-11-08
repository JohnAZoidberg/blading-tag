"""Synthetic skating data, so the tools can be tested before the firmware
and real recordings exist.

This is a rough kinematic model of one skate, not physics. It is good enough
to exercise the protocol, clock sync, gap handling and push detection, but
any thresholds tuned on it must be re-checked against real data.

Skate frame: x forward, y left, z up. The tag is mounted with a fixed random
rotation, so the analysis has to find "down" itself, as with a real mount.

One stroke cycle of the simulated foot (phase u in [0, 1)):

    0 ........ glide_end ........ contact_end ........ 1
    | set-down | glide (outside  | push (rolls onto   | recovery: foot in the
    | impact   |  edge, flat)    |  inside edge, foot |  air, lifted, swung
    |          |                 |  moves outward)    |  back under the body
    <------- wheels on the road: vibration -------->
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
from scipy import signal
from scipy.spatial.transform import Rotation

from . import protocol as p
from .session import RecordKind, SessionWriter

G = 9.80665


@dataclass
class SkaterModel:
    duration_s: float = 600.0
    still_s: float = 3.0                  # standing still at the start
    cycle_s_start: float = 1.45           # one foot's stroke period at the start
    cycle_s_end: float = 1.60             # ... and at the end (fatigue)
    speed_start_mps: float = 7.5
    speed_end_mps: float = 6.8
    contact_end: float = 0.64             # fraction of the cycle on the ground
    glide_end: float = 0.30               # glide -> push transition
    pause: tuple[float, float] | None = (0.45, 0.48)  # fraction of session standing still
    side: str = "left"
    vibration_g: float = 0.25             # road vibration RMS at 7 m/s
    seed: int = 0


@dataclass
class Truth:
    rate_hz: float
    mount_rotvec: list[float]
    set_down_s: list[float] = field(default_factory=list)   # start of each cycle (wheels touch)
    lift_off_s: list[float] = field(default_factory=list)   # end of contact
    push_start_s: list[float] = field(default_factory=list)


def _smoothstep(x):
    x = np.clip(x, 0.0, 1.0)
    return x * x * (3 - 2 * x)


def simulate_motion(m: SkaterModel, rate_hz: float = 200.0):
    """Return (t_s, accel_g (N,3), gyro_dps (N,3), speed_mps (N,), truth), in the sensor frame."""
    rng = np.random.default_rng(m.seed)
    n = int(m.duration_s * rate_hz)
    t = np.arange(n) / rate_hz
    frac = t / m.duration_s
    sgn = 1.0 if m.side == "left" else -1.0  # inside edge rolls towards the body's centre line

    skating = t >= m.still_s
    if m.pause:
        skating &= ~((frac >= m.pause[0]) & (frac < m.pause[1]))
    period = m.cycle_s_start + (m.cycle_s_end - m.cycle_s_start) * frac
    period *= 1 + 0.02 * np.sin(2 * np.pi * t / 37.0)  # a little natural wobble
    phase = np.cumsum(np.where(skating, 1.0 / (period * rate_hz), 0.0))
    # Restart the cycle cleanly after every still segment.
    starts = np.flatnonzero(np.diff(skating.astype(int)) == 1) + 1
    for s in starts:
        phase[s:] -= phase[s] - math.floor(phase[s])
    u = phase - np.floor(phase)
    u[~skating] = 0.0
    ce, ge = m.contact_end, m.glide_end
    air = skating & (u >= ce)
    contact = skating & ~air
    push = contact & (u >= ge)

    # Edge (roll), toe (pitch), splay (yaw) angles in degrees.
    w_push = _smoothstep((u - ge) / (ce - ge))
    w_air = (u - ce) / (1 - ce)
    roll = np.where(contact, 5 - 35 * w_push, -30 + 35 * _smoothstep(w_air))
    pitch = np.where(air, 18 * np.sin(np.pi * np.clip(w_air, 0, 1)) ** 2, 0.0)
    yaw = np.where(contact, 6 + 12 * w_push, 18 - 12 * _smoothstep(w_air))
    # Low-pass so starting/stopping doesn't step the angles.
    sos_ang = signal.butter(2, 8, fs=rate_hz, output="sos")
    roll, pitch, yaw = (signal.sosfiltfilt(sos_ang, np.where(skating, a, 0.0)) * sgn ** k
                        for a, k in ((roll, 1), (pitch, 0), (yaw, 1)))

    # Foot position relative to the hip (world frame), m.
    lateral = np.where(contact, 0.35 * w_push, 0.35 * (1 - _smoothstep(w_air))) * sgn
    height = np.where(air, 0.07 * np.sin(np.pi * np.clip(w_air, 0, 1)) ** 2, 0.0)
    pos = np.stack([np.zeros(n), lateral * skating, height * skating], axis=1)
    lin_world = np.gradient(np.gradient(pos, t, axis=0), t, axis=0) / G
    lin_world = signal.sosfiltfilt(signal.butter(2, 15, fs=rate_hz, output="sos"), lin_world, axis=0)

    # Body orientation and rates.
    rot = Rotation.from_euler("ZYX", np.stack([yaw, pitch, roll], axis=1), degrees=True)
    dq_rot = rot[:-1].inv() * rot[1:]
    omega = np.vstack([dq_rot.as_rotvec() * rate_hz, [0, 0, 0]])  # body-frame rad/s
    specific_world = lin_world + np.array([0, 0, 1.0])
    accel = rot.inv().apply(specific_world)

    # Set-down impact and road vibration (only while wheels are on the road).
    speed = np.where(skating, m.speed_start_mps + (m.speed_end_mps - m.speed_start_mps) * frac, 0.0)
    speed = signal.sosfiltfilt(signal.butter(1, 0.05, fs=rate_hz, output="sos"), speed)
    sos_vib = signal.butter(4, [25, 90], btype="band", fs=rate_hz, output="sos")
    vib = signal.sosfilt(sos_vib, rng.standard_normal((n, 3)), axis=0)
    vib *= m.vibration_g / vib.std(axis=0) * (speed / 7.0)[:, None] * contact[:, None]
    accel += vib
    set_down = np.flatnonzero(np.diff(air.astype(int)) == -1) + 1
    first_down = starts  # first contact after a still segment
    k = np.arange(int(0.05 * rate_hz))
    impulse = 1.5 * np.exp(-k / (0.008 * rate_hz)) * np.cos(2 * np.pi * 40 * k / rate_hz)
    for s in set_down:
        e = min(n, s + len(k))
        accel[s:e, 2] += impulse[: e - s]

    accel += rng.normal(0, 0.004, accel.shape)
    gyro = np.degrees(omega) + rng.normal(0, 0.1, omega.shape)

    # Fixed random mount.
    mount = Rotation.random(random_state=m.seed + 1)
    accel = mount.apply(accel)
    gyro = mount.apply(gyro)

    lift_off = np.flatnonzero(np.diff(air.astype(int)) == 1) + 1
    push_start = np.flatnonzero(np.diff(push.astype(int)) == 1) + 1
    truth = Truth(
        rate_hz=rate_hz,
        mount_rotvec=mount.as_rotvec().tolist(),
        set_down_s=(np.sort(np.concatenate([set_down, first_down])) / rate_hz).tolist(),
        lift_off_s=(lift_off / rate_hz).tolist(),
        push_start_s=(push_start / rate_hz).tolist(),
    )
    return t, accel, gyro, speed, truth


def quantize(accel_g, gyro_dps, accel_range_g=16, gyro_range_dps=2000) -> np.ndarray:
    raw = np.hstack([accel_g / p.accel_scale_g(accel_range_g), gyro_dps / p.gyro_scale_dps(gyro_range_dps)])
    return np.clip(np.round(raw), -32768, 32767).astype(np.int16)


@dataclass
class LinkModel:
    true_rate_hz: float = 200.0 * 1.004     # BMI270 runs 0.4 % fast
    clock_skew: float = 1 + 30e-6            # tag crystal vs phone clock
    rx_offset_ns: int = 50_000_000_000       # receiver elapsed_ns at tag time 0
    tag_boot_offset_us: int = 12_000_000     # tag uptime at START
    latency_ms: float = 4.0
    jitter_ms: float = 6.0
    drops: list[tuple[float, float]] = field(default_factory=lambda: [(200.0, 12.0)])  # (start_s, duration_s)
    buffer_capacity: int = 8000


def write_session(folder: Path, model: SkaterModel | None = None, link: LinkModel | None = None,
                  address: str = "C0:BB:4A:A7:0D:57", with_gps: bool = True) -> Truth:
    """Simulate a recording and write it as a session folder, plus ``sim_truth.json``."""
    model = model or SkaterModel()
    link = link or LinkModel()
    rng = np.random.default_rng(model.seed + 100)
    t, accel, gyro, speed, truth = simulate_motion(model, link.true_rate_hz)
    raw = quantize(accel, gyro)
    n = len(raw)
    pos = p.Position.from_label(model.side)

    start_utc = datetime(2026, 11, 7, 14, 25, 30, tzinfo=timezone.utc)
    tag_us = link.tag_boot_offset_us + np.arange(n) * 1e6 / link.true_rate_hz
    to_rx = lambda us: link.rx_offset_ns + link.clock_skew * np.asarray(us) * 1000.0
    start_rx = int(to_rx(link.tag_boot_offset_us)) - 200_000_000
    w = SessionWriter(folder, start_utc=start_utc, start_elapsed_ns=start_rx)
    info = p.Info(1, (0, 1, 0), list(p.ODR_VALUES), list(p.ACCEL_RANGES), list(p.GYRO_RANGES),
                  link.buffer_capacity)
    log = w.add_tag(address, info.pack())
    w.tags[address].simulated = True
    w.meta["activity"] = "skating"   # the model is a skater
    w.save()

    def status(state, i, fill=0, dropped=0, extra_us=0):
        return p.Status(state, p.StatusFlag.OVERFLOW if dropped else 0, 2950, 3, 200, 16, pos, 2000,
                        i, dropped, fill, int(tag_us[min(i, n - 1)] + extra_us)).pack()

    log.write(RecordKind.STATUS, status(p.State.READY, 0, extra_us=-150_000), start_rx + 50_000_000)
    log.write(RecordKind.STATUS, status(p.State.RECORDING, 0), int(to_rx(tag_us[0])) + 5_000_000)

    spp = p.samples_per_packet()
    drops = [(d0, d0 + dl) for d0, dl in link.drops]
    dropped_total = 0
    seq = 0
    i = 0
    next_status_s = 1.0
    backlog: list[tuple[int, int]] = []  # (start, count) of buffered packets during a drop
    records: list[tuple[int, int, bytes]] = []
    while i < n:
        cnt = min(spp, n - i)
        t_ready = tag_us[i + cnt - 1]  # last sample of the packet taken
        ts_s = t[i]
        in_drop = next(((a, b) for a, b in drops if a <= ts_s < b), None)
        if in_drop:
            buffered = sum(c for _, c in backlog)
            if buffered + cnt > link.buffer_capacity:
                dropped_total += cnt
            else:
                backlog.append((i, cnt))
            i += cnt
            continue
        if backlog:
            # Reconnected: Status read, then the backlog bursts out.
            rx0 = to_rx(t_ready) + 300e6
            records.append((int(rx0), RecordKind.STATUS,
                            status(p.State.RECORDING, i, sum(c for _, c in backlog), dropped_total)))
            for k, (bi, bc) in enumerate(backlog):
                pkt = p.DataPacket.pack_imu6(seq, bi, int(tag_us[bi]), raw[bi:bi + bc])
                records.append((int(rx0 + (k + 1) * 4e6), RecordKind.DATA, pkt))
                seq += 1
            backlog = []
        rx = to_rx(t_ready) + (link.latency_ms + rng.exponential(link.jitter_ms)) * 1e6
        records.append((int(rx), RecordKind.DATA, p.DataPacket.pack_imu6(seq, i, int(tag_us[i]), raw[i:i + cnt])))
        seq += 1
        if ts_s >= next_status_s:
            records.append((int(rx) + 1000, RecordKind.STATUS, status(p.State.RECORDING, i, dropped=dropped_total)))
            next_status_s = ts_s + 1.0
        i += cnt
    # STOP: final status.
    end_rx = int(to_rx(tag_us[-1])) + 50_000_000
    records.append((end_rx, RecordKind.STATUS, status(p.State.READY, n, dropped=dropped_total)))
    # Notifications arrive in order (BLE is ordered), but our latency model can
    # reorder rx times slightly; enforce monotonic receive time.
    last = 0
    for rx, kind, payload in records:
        last = max(last + 1, rx)
        log.write(kind, payload, last)

    w.tags[address].update_from_status(p.Status.parse(status(p.State.READY, n)))

    if with_gps:
        _write_gps(w, model, t, speed, to_rx(tag_us), start_utc, start_rx, rng)
    w.close(end_rx + 10_000_000)
    truth_d = asdict(truth)
    truth_d.update(model=asdict(model), link=asdict(link), samples_dropped=dropped_total)
    (Path(folder) / "sim_truth.json").write_text(json.dumps(truth_d))
    return truth


def _write_gps(w: SessionWriter, model, t, speed, rx_ns, start_utc, start_rx, rng):
    lat0, lon0 = 52.5200, 13.4050  # Tempelhofer-ish; any open space would do
    rate = 1.0 / (t[1] - t[0])
    step = int(rate)  # 1 Hz
    bearing = np.radians(30 + 40 * np.sin(2 * np.pi * t / 300.0))
    north = np.cumsum(speed * np.cos(bearing)) / rate
    east = np.cumsum(speed * np.sin(bearing)) / rate
    for k in range(0, len(t), step):
        e_ns = int(rx_ns[k]) + int(rng.normal(0, 2e6))
        utc = start_utc + timedelta(microseconds=(e_ns - start_rx) / 1e3)
        lat = lat0 + (north[k] + rng.normal(0, 2)) / 111_320
        lon = lon0 + (east[k] + rng.normal(0, 2)) / (111_320 * math.cos(math.radians(lat0)))
        w.gps_fix(elapsed_ns=e_ns, utc_ms=int(utc.timestamp() * 1000), lat=round(lat, 7), lon=round(lon, 7),
                  alt_m=round(35 + rng.normal(0, 1), 1), speed_mps=round(max(0, speed[k] + rng.normal(0, 0.15)), 2),
                  bearing_deg=round(math.degrees(bearing[k]) % 360, 1), h_acc_m=3.5, speed_acc_mps=0.3)
