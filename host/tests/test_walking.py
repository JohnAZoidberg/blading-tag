import numpy as np
import pandas as pd
import pytest

from bladetag import walking
from bladetag.explore import ACCEL, GYRO

G = 9.80665


def _realistic_pitch(u):
    """Toes down to about -60° after toe-off, up to +20° at u = 0.8, where
    the heel strikes. 0 at both ends, as the foot is flat in stance."""
    return np.radians(np.sin(np.pi * u) ** 2 * (-201 + 324 * u))


def synthetic_walk(stride_m=1.4, period_s=1.1, n_strides=20, fs=200.0, rest_s=3.0, seed=0,
                   realistic=False):
    """An ankle tag: planted for 60 % of each stride, then it swings forward
    by ``stride_m``, lifting 10 cm and pitching up to 35°, mounted at a
    random angle, with sensor noise and a gyro bias.

    With ``realistic``, the pitch follows :func:`_realistic_pitch` instead
    and the heel strikes with a 3 g jolt at 80 % of the swing."""
    rng = np.random.default_rng(seed)
    t = np.arange(0, rest_s + n_strides * period_s + 0.7 * period_s, 1 / fs)
    swing = 0.4 * period_s
    acc_w = np.zeros((len(t), 3))
    theta = np.zeros(len(t))
    omega = np.zeros(len(t))
    for k, tk in enumerate(t):
        ph = (tk - rest_s) % period_s - 0.6 * period_s
        if tk < rest_s or ph < 0 or (tk - rest_s) // period_s >= n_strides:
            continue
        u, w = ph / swing, 2 * np.pi / swing
        acc_w[k, 0] = stride_m / swing**2 * 2 * np.pi * np.sin(2 * np.pi * u)
        acc_w[k, 2] = 0.05 * w**2 * np.cos(2 * np.pi * u)
        # Rotating about +y turns the toes down, so toes-up pitch is -theta.
        theta[k] = -_realistic_pitch(u) if realistic else 0.6 * np.sin(np.pi * u) ** 2
        omega[k] = 0.6 * np.pi / swing * np.sin(2 * np.pi * u)
    if realistic:
        omega = np.gradient(theta, 1 / fs)
        ph = (t - rest_s) % period_s - 0.6 * period_s
        hit = np.nonzero((t >= rest_s) & (np.abs(ph - 0.8 * swing) < 0.5 / fs)
                         & ((t - rest_s) // period_s < n_strides))[0]
        acc_w[hit, 2] += 3 * G       # up, then down again: no net change in speed
        acc_w[hit + 1, 2] -= 3 * G
    f_w = acc_w + [0, 0, G]
    c, s = np.cos(theta), np.sin(theta)
    # Pitch about y: body = Ry(theta)^T world.
    f_b = np.stack([c * f_w[:, 0] - s * f_w[:, 2], f_w[:, 1], s * f_w[:, 0] + c * f_w[:, 2]], axis=1)
    w_b = np.stack([np.zeros_like(omega), omega, np.zeros_like(omega)], axis=1)
    q, _ = np.linalg.qr(rng.normal(size=(3, 3)))
    mount = q * np.sign(np.linalg.det(q))
    acc = f_b @ mount.T / G + rng.normal(0, 0.01, (len(t), 3))
    gyr = np.degrees(w_b @ mount.T) + rng.normal(0, 0.5, (len(t), 3)) + [0.8, -0.5, 0.3]
    df = pd.DataFrame({"t_s": t, "sample_index": np.arange(len(t)), "segment": 0})
    df[ACCEL] = acc
    df[GYRO] = gyr
    df["a_mag"] = np.linalg.norm(acc, axis=1)
    df["g_mag"] = np.linalg.norm(gyr, axis=1)
    df["a_clip"] = df["g_clip"] = False
    return df, fs


def test_stride_length_from_synthetic_walk():
    df, fs = synthetic_walk()
    st = walking.strides(df, fs)
    assert len(st) >= 18
    assert st["stride_m"].median() == pytest.approx(1.4, rel=0.03)
    assert st["step_m"].median() == pytest.approx(0.7, rel=0.03)
    assert st["period_s"].median() == pytest.approx(1.1, abs=0.02)
    assert st["dz_m"].abs().median() < 0.05
    assert st["speed_kmh"].median() == pytest.approx(1.4 / 1.1 * 3.6, rel=0.05)


def test_gait_measures_from_synthetic_walk():
    df, fs = synthetic_walk(realistic=True)
    st = walking.strides(df, fs)
    assert len(st) >= 18
    assert st["stride_m"].median() == pytest.approx(1.4, rel=0.05)
    u = np.linspace(0, 1, 1001)
    assert st["strike_deg"].median() == pytest.approx(np.degrees(_realistic_pitch(0.8)), abs=3)
    assert st["toeoff_deg"].median() == pytest.approx(np.degrees(_realistic_pitch(u)).min(), abs=3)
    assert st["impact_g"].median() > 3
    assert st["lift_cm"].median() == pytest.approx(10, abs=1.5)
    # Swing from the fastest pitch-down to the jolt, about 0.15-0.8 of the 0.44 s swing.
    assert 0.25 < st["swing_s"].median() < 0.4
    assert 60 < st["stance_pct"].median() < 80


def test_strides_dont_cross_gaps():
    df, fs = synthetic_walk()
    df.loc[df["t_s"] >= 10, "segment"] = 1
    whole = walking.strides(synthetic_walk()[0], fs)
    split = walking.strides(df, fs)
    assert len(split) == len(whole) - 1


def test_describe():
    df, fs = synthetic_walk()
    lines = walking.describe(walking.strides(df, fs))
    assert "about 0.7" in "\n".join(lines)
    assert "none found" in walking.describe(walking.strides(df.iloc[:400], fs))[0]


def test_skating_finds_no_strides(tmp_path):
    # Gliding looks planted (steady, ~1 g) but vibrates; it must not count.
    from bladetag import sim
    from bladetag.explore import load

    sim.write_session(tmp_path / "s", sim.SkaterModel(duration_s=120, seed=2), sim.LinkModel(), with_gps=True)
    df, fs, _ = load(tmp_path / "s")
    assert walking.strides(df, fs).empty


def test_html_has_step_panel_data():
    from bladetag.explore_html import payload

    df, fs = synthetic_walk()
    st = walking.strides(df, fs)
    meta, _ = payload(df, fs, strides=st)
    assert len(meta["strides"]["t"]) == len(st)
    assert np.median(meta["strides"]["step"]) == pytest.approx(0.7, rel=0.03)
    assert "strides" not in payload(df, fs)[0]


def test_compare_at_common_speed():
    a = walking.strides(synthetic_walk(realistic=True, n_strides=30)[0], 200.0)
    b = walking.strides(synthetic_walk(realistic=True, n_strides=30, seed=1)[0], 200.0)
    c = walking.strides(synthetic_walk(realistic=True, n_strides=30, stride_m=1.2, seed=2)[0], 200.0)
    per, groups, speed = walking.compare([("A", "a1", a), ("A", "a2", b), ("B", "c", c)], band_kmh=2)
    assert list(groups["group"]) == ["A", "B"]
    assert list(groups["sessions"]) == [2, 1]
    assert groups.loc[0, "strike_deg"] == pytest.approx(20, abs=3)
    assert groups.loc[0, "sd_strike_deg"] < 1
    assert "sd_strike_deg" not in groups.columns or pd.isna(groups.loc[1, "sd_strike_deg"])
    # Shorter strides at the same period are slower, so far from the common speed.
    assert per.loc[2, "stride_m"] < per.loc[0, "stride_m"]
    text = walking.format_compare(per, groups, speed)
    assert "between walks" in text and "a2" in text


def test_at_speed_needs_enough_strides():
    st = walking.strides(synthetic_walk(realistic=True)[0], 200.0)
    r = walking.at_speed(st, 20.0)
    assert r["n"] == 0 and np.isnan(r["strike_deg"])
