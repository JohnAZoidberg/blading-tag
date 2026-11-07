import numpy as np
import pandas as pd
import pytest

from bladetag import walking
from bladetag.explore import ACCEL, GYRO

G = 9.80665


def synthetic_walk(stride_m=1.4, period_s=1.1, n_strides=20, fs=200.0, rest_s=3.0, seed=0):
    """An ankle tag: planted for 60 % of each stride, then it swings forward
    by ``stride_m``, lifting 10 cm and pitching up to 35°, mounted at a
    random angle, with sensor noise and a gyro bias."""
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
        theta[k] = 0.6 * np.sin(np.pi * u) ** 2
        omega[k] = 0.6 * np.pi / swing * np.sin(2 * np.pi * u)
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
