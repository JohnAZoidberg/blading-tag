"""How the skate moved, in 3D, from one tag on it.

A stroke cycle (wheel set-down to the next set-down, from
:func:`analysis.detect_strokes`) is turned into a path of the skate relative
to the skater:

1. **Orientation.** A Mahony filter integrates the gyro and pulls the tilt
   towards the low-passed accelerometer. Pushing accelerates the skate hard
   enough to bias that pull by ~15°, so the tilt is then corrected again so
   that the world-frame acceleration, averaged over 3 s (two strokes), points
   straight up: over whole strokes the skater's speed barely changes.
   Heading isn't observable without a magnetometer and is re-anchored every
   stroke (step 3).
2. **Path.** Per stroke, the world-frame acceleration is integrated twice.
   Horizontally the stroke is assumed periodic: the velocity drift is
   removed linearly and the mean (the skater's own speed) subtracted, which
   leaves the skate's motion relative to someone moving at constant velocity.
   Vertically the wheels are on the road during contact (zero vertical
   velocity), so only the air phase is integrated, from lift-off to set-down.
3. **Skater frame.** The horizontal path of a stroke is long and thin: the
   push goes sideways. Its long axis is the skater's lateral direction, and
   the side the skate moves to while its wheels are down is outward. Forward
   follows from that and from which foot the tag is on. The heading is
   smoothed over a few strokes.
4. **Mount.** The same lateral direction, seen in the sensor frame during
   contact, gives the skate's axes in sensor coordinates. Up comes from rest
   periods (standing on level ground) when there are any. ``Mount.agreement``
   (0..1) says how consistently the strokes agree on it.

The skate's position is the tag's; turning the skate moves the tag a few cm
around the wheels, which is ignored. Lateral sweep includes the skater's
body sway, since the reference moves in a straight line at constant speed.
"""

from __future__ import annotations

import base64
import json
import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import signal
from scipy.ndimage import median_filter, uniform_filter1d

from .analysis import Result, analyze
from .explore import ACCEL, GYRO, sample_frame, still_mask
from .session import Session

G = 9.80665
TEMPLATE = Path(__file__).with_name("viz3d_template.html")


@dataclass
class Params:
    acc_lp_hz: float = 3.0       # accelerometer low-pass for the Mahony tilt pull
    kp: float = 0.3              # Mahony proportional gain (1/s)
    ki: float = 0.005            # Mahony integral gain (gyro bias)
    tilt_win_s: float = 3.0      # world acceleration averaged over this is vertical
    heading_strokes: int = 5     # median over this many strokes
    out_hz: float = 50.0         # frames written to the page
    phase_points: int = 120      # points per stroke in the typical-stroke loops
    fatigue_frac: float = 0.2    # early/late = first/last 20 % of strokes


@dataclass
class Mount:
    forward: np.ndarray  # skate axes in sensor coordinates (unit vectors)
    left: np.ndarray
    up: np.ndarray
    up_from: str         # "rest" or "contact"
    agreement: float     # resultant length of the per-stroke lateral estimates
    yaw_fix_deg: float   # yaw correction from the wheel constraint
    wheel_corr: float = float("nan")  # skate yaw vs. direction of travel during contact

    def matrix(self) -> np.ndarray:
        """Skate -> sensor: columns are the skate's forward, left, up."""
        return np.column_stack([self.forward, self.left, self.up])

    def rotated(self, yaw: float) -> "Mount":
        """The skate axes turned by ``yaw`` (rad) about its up axis."""
        c, s = math.cos(yaw), math.sin(yaw)
        f, l = c * self.forward + s * self.left, -s * self.forward + c * self.left
        return Mount(f, l, self.up, self.up_from, self.agreement, self.yaw_fix_deg + math.degrees(yaw))


@dataclass
class FootMotion:
    name: str
    meta: dict
    side: int                 # +1 left foot, -1 right foot
    t: np.ndarray             # s since session start
    pos: np.ndarray           # (N, 3) m, skater frame: x forward, y left, z up
    quat: np.ndarray          # (N, 4) x, y, z, w: skate -> skater frame
    contact: np.ndarray       # bool
    stroke: np.ndarray        # int, stroke row, -1 outside strokes
    dist_m: np.ndarray        # along-track distance (GPS)
    strokes: pd.DataFrame
    typical: dict
    mount: Mount
    result: Result
    warnings: list


# --- small rotation helpers -------------------------------------------------


def _skew(v: np.ndarray) -> np.ndarray:
    k = np.zeros(v.shape[:-1] + (3, 3))
    k[..., 0, 1], k[..., 0, 2] = -v[..., 2], v[..., 1]
    k[..., 1, 0], k[..., 1, 2] = v[..., 2], -v[..., 0]
    k[..., 2, 0], k[..., 2, 1] = -v[..., 1], v[..., 0]
    return k


def _rotvec_to_matrix(rv: np.ndarray) -> np.ndarray:
    th = np.linalg.norm(rv, axis=-1)[..., None, None]
    k = _skew(rv / np.maximum(th[..., 0], 1e-12))
    return np.eye(3) + np.sin(th) * k + (1 - np.cos(th)) * (k @ k)


def _rot_z(psi: np.ndarray) -> np.ndarray:
    c, s = np.cos(psi), np.sin(psi)
    r = np.zeros(psi.shape + (3, 3))
    r[..., 0, 0], r[..., 0, 1], r[..., 1, 0], r[..., 1, 1], r[..., 2, 2] = c, -s, s, c, 1
    return r


def _matrix_to_quat(m: np.ndarray) -> np.ndarray:
    """(N, 3, 3) -> (N, 4) as x, y, z, w (three.js order)."""
    w = np.sqrt(np.maximum(0, 1 + m[:, 0, 0] + m[:, 1, 1] + m[:, 2, 2])) / 2
    x = np.sqrt(np.maximum(0, 1 + m[:, 0, 0] - m[:, 1, 1] - m[:, 2, 2])) / 2
    y = np.sqrt(np.maximum(0, 1 - m[:, 0, 0] + m[:, 1, 1] - m[:, 2, 2])) / 2
    z = np.sqrt(np.maximum(0, 1 - m[:, 0, 0] - m[:, 1, 1] + m[:, 2, 2])) / 2
    x = np.copysign(x, m[:, 2, 1] - m[:, 1, 2])
    y = np.copysign(y, m[:, 0, 2] - m[:, 2, 0])
    z = np.copysign(z, m[:, 1, 0] - m[:, 0, 1])
    q = np.column_stack([x, y, z, w])
    return q / np.linalg.norm(q, axis=1, keepdims=True)


def _level(a: np.ndarray) -> np.ndarray:
    """Rotation taking the specific force ``a`` onto +z (heading arbitrary)."""
    a = a / np.linalg.norm(a)
    v, c = np.cross(a, [0.0, 0.0, 1.0]), float(a[2])
    s = np.linalg.norm(v)
    if s < 1e-9:
        return np.eye(3) if c > 0 else np.diag([1.0, -1.0, -1.0])
    k = _skew(v)
    return np.eye(3) + k + k @ k * ((1 - c) / s ** 2)


# --- 1. orientation -----------------------------------------------------------


def _mahony(acc_lp: np.ndarray, gyr: np.ndarray, dt: float, p: Params) -> np.ndarray:
    """Sensor -> world rotation per sample. Plain floats: much faster than
    numpy for one 3x3 at a time."""
    n = len(gyr)
    out = np.empty((n, 3, 3))
    r = _level(acc_lp[: max(1, min(n, int(1 / dt)))].mean(axis=0))
    r00, r01, r02, r10, r11, r12, r20, r21, r22 = r.ravel().tolist()
    bx = by = bz = 0.0
    ax_, ay_, az_ = acc_lp[:, 0].tolist(), acc_lp[:, 1].tolist(), acc_lp[:, 2].tolist()
    gx_, gy_, gz_ = gyr[:, 0].tolist(), gyr[:, 1].tolist(), gyr[:, 2].tolist()
    kp, ki = p.kp, p.ki
    sin, cos, sqrt = math.sin, math.cos, math.sqrt
    for i in range(n):
        ax, ay, az = ax_[i], ay_[i], az_[i]
        nrm = sqrt(ax * ax + ay * ay + az * az)
        ex = ey = ez = 0.0
        if 0.7 < nrm < 1.3:
            ax, ay, az = ax / nrm, ay / nrm, az / nrm
            ux, uy, uz = r20, r21, r22             # world up in sensor frame
            ex, ey, ez = ay * uz - az * uy, az * ux - ax * uz, ax * uy - ay * ux
            bx += ki * ex * dt; by += ki * ey * dt; bz += ki * ez * dt
        wx, wy, wz = gx_[i] + kp * ex + bx, gy_[i] + kp * ey + by, gz_[i] + kp * ez + bz
        th = sqrt(wx * wx + wy * wy + wz * wz) * dt
        if th > 0:
            k = th / dt
            kx, ky, kz = wx / k, wy / k, wz / k
            s, c1 = sin(th), 1 - cos(th)
            # Rodrigues: d = I + s K + c1 K^2
            d00 = 1 - c1 * (ky * ky + kz * kz); d11 = 1 - c1 * (kx * kx + kz * kz); d22 = 1 - c1 * (kx * kx + ky * ky)
            d01 = -s * kz + c1 * kx * ky; d10 = s * kz + c1 * kx * ky
            d02 = s * ky + c1 * kx * kz; d20 = -s * ky + c1 * kx * kz
            d12 = -s * kx + c1 * ky * kz; d21 = s * kx + c1 * ky * kz
            r00, r01, r02 = r00 * d00 + r01 * d10 + r02 * d20, r00 * d01 + r01 * d11 + r02 * d21, r00 * d02 + r01 * d12 + r02 * d22
            r10, r11, r12 = r10 * d00 + r11 * d10 + r12 * d20, r10 * d01 + r11 * d11 + r12 * d21, r10 * d02 + r11 * d12 + r12 * d22
            r20, r21, r22 = r20 * d00 + r21 * d10 + r22 * d20, r20 * d01 + r21 * d11 + r22 * d21, r20 * d02 + r21 * d12 + r22 * d22
        out[i] = ((r00, r01, r02), (r10, r11, r12), (r20, r21, r22))
    return out


def orientation(acc_g: np.ndarray, gyr_dps: np.ndarray, fs: float, segment: np.ndarray,
                bias_dps: np.ndarray, p: Params = Params()) -> np.ndarray:
    """Sensor -> world (z up, heading arbitrary and drifting), per sample."""
    gyr = np.radians(gyr_dps - bias_dps)
    sos = signal.butter(2, p.acc_lp_hz, fs=fs, output="sos")
    r = np.empty((len(acc_g), 3, 3))
    win = max(1, int(p.tilt_win_s * fs))
    for s in np.unique(segment):
        sl = np.flatnonzero(segment == s)
        sl = slice(sl[0], sl[-1] + 1)
        a = acc_g[sl]
        alp = signal.sosfiltfilt(sos, a, axis=0) if len(a) > 15 else a
        rs = _mahony(alp, gyr[sl], 1 / fs, p)
        # Tilt correction: the windowed mean of the world acceleration is vertical.
        for _ in range(3):
            f = np.einsum("nij,nj->ni", rs, a)
            m = uniform_filter1d(f, min(win, len(f)), axis=0, mode="nearest")
            m /= np.linalg.norm(m, axis=1, keepdims=True)
            axis = np.cross(m, [0.0, 0.0, 1.0])
            sn = np.linalg.norm(axis, axis=1, keepdims=True)
            rv = axis / np.maximum(sn, 1e-12) * np.arcsin(np.clip(sn, 0, 1))
            rs = _rotvec_to_matrix(rv) @ rs
        r[sl] = rs
    return r


# --- 2./3. per-stroke path and the skater frame --------------------------------


def _integrate(x: np.ndarray, dt: float) -> np.ndarray:
    return np.concatenate([np.zeros((1,) + x.shape[1:]), np.cumsum((x[1:] + x[:-1]) * (dt / 2), axis=0)])


def _detrend_end(x: np.ndarray) -> np.ndarray:
    """Remove a linear ramp so the last value equals the first."""
    return x - np.outer(np.linspace(0, 1, len(x)), x[-1] - x[0]).reshape(x.shape)


def _stroke_path(aw: np.ndarray, lo: int | None, dt: float) -> np.ndarray:
    """Relative path (n, 3) of one stroke from world acceleration (m/s^2,
    gravity removed). ``lo``: lift-off index within the stroke, or None."""
    h = aw[:, :2]
    v = _detrend_end(_integrate(h, dt))
    v -= v.mean(axis=0)
    ph = _detrend_end(_integrate(v, dt))
    ph -= ph.mean(axis=0)
    z = np.zeros(len(aw))
    if lo is not None and 2 < lo < len(aw) - 3:
        vz = _integrate(aw[lo:, 2], dt)
        vz = vz - np.linspace(0, 1, len(vz)) * vz[-1]
        zz = _integrate(vz, dt)
        z[lo:] = zz - np.linspace(0, 1, len(zz)) * zz[-1]
    else:  # no contact information: periodic like the horizontal
        vz = _detrend_end(_integrate(aw[:, 2], dt))
        vz -= vz.mean()
        z = _detrend_end(_integrate(vz, dt))
        z -= z.min()
    return np.column_stack([ph, z])


def _side(position: str) -> tuple[int, str | None]:
    if "left" in position:
        return 1, None
    if "right" in position:
        return -1, None
    return 1, f"tag position is {position!r}: assumed left foot (outward = left)"


def foot_motion(sess: Session, tag=None, p: Params = Params(), result: Result | None = None) -> FootMotion:
    r = result or analyze(sess, tag)
    tag = r.tag
    warnings = list(r.warnings)
    side, w = _side(tag.meta.position)
    if w:
        warnings.append(w)
    df = sample_frame(sess, tag, r.clock)
    fs, dt = r.clock.rate_hz, 1 / r.clock.rate_hz
    t = df["t_s"].to_numpy()
    acc = df[ACCEL].to_numpy()
    gyr = df[GYRO].to_numpy()
    seg = df["segment"].to_numpy()
    still = still_mask(df, fs)
    bias = gyr[still].mean(axis=0) if still.sum() > 3 * fs else np.zeros(3)
    rw = orientation(acc, gyr, fs, seg, bias, p)
    aw = np.einsum("nij,nj->ni", rw, acc) * G - [0.0, 0.0, G]
    contact = r.signals.contact.astype(bool)

    st = r.strokes.reset_index(drop=True)
    n = len(t)
    i0s = np.searchsorted(t, st["t_s"].to_numpy())
    i1s = np.searchsorted(t, (st["t_s"] + st["period_s"]).to_numpy())
    los = np.searchsorted(t, st["lift_off_s"].fillna(-1).to_numpy())
    world = np.full((n, 3), np.nan)
    stroke_id = np.full(n, -1, dtype=np.int64)
    psi_k, lat_s, centres, ok = [], [], [], []
    for k, (i0, i1, lo) in enumerate(zip(i0s, i1s, los)):
        i1 = min(i1, n - 1)
        if i1 - i0 < 10:
            ok.append(False)
            continue
        lo_rel = int(lo - i0) if i0 < lo < i1 else None
        path = _stroke_path(aw[i0:i1 + 1], lo_rel, dt)
        world[i0:i1] = path[:-1]
        stroke_id[i0:i1] = k
        cov = np.cov(path[:, :2].T)
        _, vecs = np.linalg.eigh(cov)
        lat = np.array([vecs[0, 1], vecs[1, 1], 0.0])
        c_end = lo_rel if lo_rel is not None else (i1 - i0) // 2
        if (path[c_end, :2] - path[0, :2]) @ lat[:2] < 0:  # wheels down = moving outward
            lat = -lat
        left = side * lat
        fwd = np.cross(left, [0.0, 0.0, 1.0])
        psi_k.append(math.atan2(fwd[1], fwd[0]))
        centres.append(t[(i0 + i1) // 2])
        cs = slice(i0, i0 + c_end)
        ls = np.einsum("nji,j->ni", rw[cs], lat).mean(axis=0)
        lat_s.append(ls / np.linalg.norm(ls))
        ok.append(True)
    if not psi_k:
        raise ValueError("no stroke cycles to show (is this a skating session?)")

    # Heading of the skater, smoothed over strokes, at every sample.
    psi = np.unwrap(np.array(psi_k))
    if len(psi) >= p.heading_strokes:
        psi = median_filter(psi, p.heading_strokes, mode="nearest")
    psi_t = np.interp(t, np.array(centres), psi)
    rz = _rot_z(-psi_t)
    pos = np.einsum("nij,nj->ni", rz, np.nan_to_num(world))

    # Mount: skate axes in the sensor frame.
    lat_s = np.array(lat_s)
    mean_lat = lat_s.mean(axis=0)
    agreement = float(np.linalg.norm(mean_lat))
    if still.sum() > 3 * fs:
        up_s, up_from = acc[still].mean(axis=0), "rest"
    else:
        up_s, up_from = rw[contact][:, 2, :].mean(axis=0), "contact"
    up_s /= np.linalg.norm(up_s)
    out_s = mean_lat - (mean_lat @ up_s) * up_s
    out_s /= np.linalg.norm(out_s)
    left_s = side * out_s
    fwd_s = np.cross(left_s, up_s)
    mount = Mount(fwd_s, left_s, up_s, up_from, agreement, 0.0)
    if agreement < 0.8:
        warnings.append(f"strokes disagree on the skate's lateral axis (agreement {agreement:.2f}): "
                        "the skate's orientation may be off")

    r_body = rz @ rw @ mount.matrix()          # skate -> skater frame

    gps = r.gps
    if gps is not None:
        dist = np.interp(t, gps["t_s"], gps["dist_m"])
        speed = np.interp(t, gps["t_s"], gps["speed_mps"].interpolate().bfill().ffill())
        # The mount above assumes the skate points along the travel
        # direction on average while its wheels are down. Wheels only roll
        # along the skate, though, so during contact the skate points where
        # it's going: the skater's speed plus the skate's own sideways motion.
        # The median difference is the mount's yaw error.
        v = np.gradient(pos, t, axis=0)
        use = contact & (stroke_id >= 0) & (speed > 2.0)
        if use.sum() > 5 * fs:
            yaw_meas = np.arctan2(r_body[:, 1, 0], r_body[:, 0, 0])
            yaw_pred = np.arctan2(v[:, 1], speed + v[:, 0])
            d = np.angle(np.exp(1j * (yaw_meas - yaw_pred)[use]))
            mount = mount.rotated(-float(np.median(d)))
            r_body = rz @ rw @ mount.matrix()
            ym = np.arctan2(r_body[use, 1, 0], r_body[use, 0, 0])
            mount.wheel_corr = float(np.corrcoef(ym, yaw_pred[use])[0, 1])
    else:
        dist = np.zeros(n)
        warnings.append("no GPS: the over-ground view uses no forward speed, and the skate's yaw "
                        "on the tag isn't calibrated")
    quat = _matrix_to_quat(r_body)

    strokes = _stroke_table(st, t, pos, r_body, contact, stroke_id, side, ok)
    typical = _typical(st, t, pos, r_body, stroke_id, i0s, i1s, los, strokes, p)
    meta = {
        "start_utc": sess.meta.get("start_utc"),
        "activity": sess.activity,
        "gear": sess.gear,
        "notes": sess.meta.get("notes", ""),
        "position": tag.meta.position,
    }
    return FootMotion(sess.path.name, meta, side, t, pos, quat, contact, stroke_id, dist,
                      strokes, typical, mount, r, warnings)


# --- per-stroke numbers -------------------------------------------------------


def _angles(r_body: np.ndarray, side: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Edge (inward lean, deg), splay (toe out, deg) and pitch (toe up, deg)."""
    f, u = r_body[:, :, 0], r_body[:, :, 2]
    yaw = np.degrees(np.arctan2(f[:, 1], f[:, 0]))
    fh = f.copy()
    fh[:, 2] = 0
    fh /= np.maximum(np.linalg.norm(fh, axis=1, keepdims=True), 1e-9)
    left_level = np.cross([0.0, 0.0, 1.0], fh)
    lean_left = np.degrees(np.arctan2(np.einsum("ni,ni->n", u, left_level), u[:, 2]))
    pitch = np.degrees(np.arcsin(np.clip(f[:, 2], -1, 1)))
    return -side * lean_left, side * yaw, pitch


STROKE_COLUMNS = ["stroke", "t_s", "period_s", "speed_kmh", "contact_frac", "sweep_cm", "out_cm", "fore_aft_cm",
                  "lift_cm", "edge_deg", "splay_deg", "push_speed_mps", "push_rate_spm", "dist_per_push_m"]


def _stroke_table(st, t, pos, r_body, contact, stroke_id, side, ok) -> pd.DataFrame:
    edge, splay, _ = _angles(r_body, side)
    rows = []
    for k in range(len(st)):
        if not ok[k]:
            continue
        sel = stroke_id == k
        if sel.sum() < 10:
            continue
        y, z, c = pos[sel, 1] * side, pos[sel, 2], contact[sel]
        vy = np.gradient(y, t[sel])
        row = st.iloc[k]
        rows.append({
            "stroke": k,
            "t_s": row["t_s"],
            "period_s": row["period_s"],
            "speed_kmh": row["speed_mps"] * 3.6,
            "contact_frac": row["contact_frac"],
            "sweep_cm": 100 * float(np.ptp(y)),
            "out_cm": 100 * float(y.max() - y[0]),        # outward from set-down to the widest point
            "fore_aft_cm": 100 * float(np.ptp(pos[sel, 0])),
            "lift_cm": 100 * float(z.max()),
            "edge_deg": float(np.percentile(edge[sel][c], 95)) if c.any() else np.nan,
            "splay_deg": float(np.median(splay[sel][c])) if c.any() else np.nan,
            "push_speed_mps": float(vy[c].max()) if c.any() else np.nan,
            "push_rate_spm": row["push_rate_spm"],
            "dist_per_push_m": row["dist_per_push_m"],
        })
    return pd.DataFrame(rows, columns=STROKE_COLUMNS)


def stroke_motion(sess: Session, result: Result | None = None, tag=None, p: Params = Params()) -> pd.DataFrame:
    """Per-stroke numbers from the 3D path, one row per stroke cycle of
    ``analysis.analyze(sess).strokes`` that could be integrated (``stroke`` is
    the row index there, ``t_s`` the same set-down time, so merge on either).

    sweep_cm: sideways range of the skate relative to the skater (includes
    body sway); out_cm: outward from set-down to the widest point;
    fore_aft_cm: forward-backward range; lift_cm: highest point in the air;
    edge_deg: inside edge (95th percentile while the wheels are down);
    splay_deg: toe out (median while the wheels are down); push_speed_mps:
    fastest outward speed of the skate while the wheels are down.
    """
    return foot_motion(sess, tag, p, result).strokes


def _typical(st, t, pos, r_body, stroke_id, i0s, i1s, los, strokes, p: Params) -> dict:
    """Mean stroke (path and skate angles over phase 0..1 from set-down) for
    all strokes and for the first and last ``fatigue_frac`` of them."""
    m = p.phase_points
    phase = np.linspace(0, 1, m, endpoint=False)
    loops, quats, lift = [], [], []
    for k in range(len(st)):
        sel = np.flatnonzero(stroke_id == k)
        if len(sel) < 20:
            continue
        ph = (t[sel] - t[sel[0]]) / (t[sel[-1]] - t[sel[0]] + 1e-9)
        loops.append(np.column_stack([np.interp(phase, ph, pos[sel, j]) for j in range(3)]))
        # Rotation matrices averaged column-wise, re-orthonormalised below.
        quats.append(np.stack([np.column_stack([np.interp(phase, ph, r_body[sel, i, j]) for j in range(3)])
                               for i in range(3)], axis=1))
        lo = los[k]
        lift.append((lo - i0s[k]) / max(1, i1s[k] - i0s[k]) if i0s[k] < lo < i1s[k] else np.nan)
    if not loops:
        return {}
    loops, mats, lift = np.array(loops), np.array(quats), np.array(lift)
    nk = len(loops)
    cut = max(1, int(round(nk * p.fatigue_frac)))
    out = {}
    for name, sl in (("all", slice(None)), ("early", slice(0, cut)), ("late", slice(nk - cut, None))):
        mm = mats[sl].mean(axis=0)  # (m, 3, 3)
        u, _, vt = np.linalg.svd(mm)
        rot = u @ vt
        out[name] = {
            "strokes": int(len(loops[sl])),
            "pos": np.round(loops[sl].mean(axis=0), 4).tolist(),
            "quat": np.round(_matrix_to_quat(rot), 4).tolist(),
            "lift_phase": float(np.nanmedian(lift[sl])) if np.isfinite(lift[sl]).any() else None,
        }
    return out


def describe(fm: FootMotion) -> list[str]:
    s, mt = fm.strokes, fm.mount
    lines = [
        f"{fm.name}: {len(s)} strokes shown, {'left' if fm.side > 0 else 'right'} foot",
        f"  mount         forward {np.round(mt.forward, 2)}, left {np.round(mt.left, 2)}, up {np.round(mt.up, 2)}"
        f" (sensor axes; up from {mt.up_from}, strokes agree {mt.agreement:.2f})",
        f"  wheel check   yaw corrected {mt.yaw_fix_deg:+.0f}°; skate yaw follows the direction of travel "
        f"during contact with r = {mt.wheel_corr:.2f}",
    ]
    if len(s):
        med = s.median(numeric_only=True)
        lines.append(f"  per stroke    sweep {med.sweep_cm:.0f} cm, outward {med.out_cm:.0f} cm, lift {med.lift_cm:.1f} cm, "
                     f"edge {med.edge_deg:.0f}°, toe-out {med.splay_deg:.0f}° (medians)")
    lines += [f"  warning: {w}" for w in fm.warnings]
    return lines


# --- the page -----------------------------------------------------------------


def _b64(a: np.ndarray) -> str:
    return base64.b64encode(np.ascontiguousarray(a).tobytes()).decode("ascii")


def page_data(fm: FootMotion, p: Params = Params()) -> dict:
    fs = fm.result.clock.rate_hz
    step = max(1, int(round(fs / p.out_hz)))
    idx = np.arange(0, len(fm.t), step)
    pos_mm = np.clip(np.round(fm.pos[idx] * 1000), -32767, 32767).astype("<i2")
    q = np.round(fm.quat[idx] * 32767).astype("<i2")
    strokes = fm.strokes.replace({np.nan: None})
    return {
        "name": fm.name,
        "meta": fm.meta,
        "side": fm.side,
        "n": int(len(idx)),
        "t": _b64(fm.t[idx].astype("<f4")),
        "pos_mm": _b64(pos_mm),
        "quat": _b64(q),
        "dist": _b64(fm.dist_m[idx].astype("<f4")),
        "contact": _b64(fm.contact[idx].astype(np.uint8)),
        "stroke": _b64(fm.stroke[idx].astype("<i2")),
        "strokes": {c: [None if v is None else round(float(v), 3) for v in strokes[c]] for c in STROKE_COLUMNS},
        "typical": fm.typical,
        "summary": {
            "push_rate_spm": fm.result.summary.get("push_rate_spm"),
            "distance_km": fm.result.summary.get("distance_km"),
            "avg_moving_speed_kmh": fm.result.summary.get("avg_moving_speed_kmh"),
            "dist_per_push_m": fm.result.summary.get("dist_per_push_m"),
            "duration_s": fm.result.summary.get("duration_s"),
        },
        "mount": {"agreement": round(fm.mount.agreement, 3), "up_from": fm.mount.up_from,
                  "yaw_fix_deg": round(fm.mount.yaw_fix_deg, 1),
                  "wheel_corr": None if math.isnan(fm.mount.wheel_corr) else round(fm.mount.wheel_corr, 2)},
        "warnings": fm.warnings,
    }


def write_html(motions: list[FootMotion], path: str | Path, title: str = "Skate strokes", p: Params = Params()) -> Path:
    data = [page_data(fm, p) for fm in motions]
    blob = json.dumps({"title": title, "sessions": data}, separators=(",", ":"), allow_nan=False)
    html = TEMPLATE.read_text().replace("/*__DATA__*/null", blob.replace("</", "<\\/"))
    path = Path(path)
    path.write_text(html)
    return path
