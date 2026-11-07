"""Plots for analysis results (matplotlib)."""

from __future__ import annotations

import numpy as np

from .analysis import Result


def _rolling(y, n):
    import pandas as pd

    return pd.Series(y).rolling(n, center=True, min_periods=max(1, n // 3)).median().to_numpy()


def plot_session(r: Result, path=None, show: bool = False):
    import matplotlib.pyplot as plt

    s = r.strokes
    has_gps = r.gps is not None and s["speed_mps"].notna().any()
    x = s["dist_km"] if has_gps else s["t_s"] / 60
    xlabel = "distance (km)" if has_gps else "time (min)"
    n = 15  # rolling window in strokes

    fig, ax = plt.subplots(2, 2, figsize=(13, 8), constrained_layout=True)
    fig.suptitle(f"{r.summary['tag']} ({r.summary['position']})")

    a = ax[0, 0]
    a.plot(x, s["push_rate_spm"], ".", ms=2, alpha=0.3, color="C0")
    a.plot(x, _rolling(s["push_rate_spm"], n), color="C0", label="push rate")
    a.set_ylabel("push rate (/min, both feet)")
    a.set_xlabel(xlabel)
    if has_gps:
        b = a.twinx()
        b.plot(x, _rolling(s["speed_mps"] * 3.6, n), color="C1", label="speed")
        b.set_ylabel("speed (km/h)", color="C1")
    a.set_title("push rate and speed")

    a = ax[0, 1]
    if has_gps:
        a.plot(x, s["dist_per_push_m"], ".", ms=2, alpha=0.3, color="C2")
        a.plot(x, _rolling(s["dist_per_push_m"], n), color="C2")
        a.set_ylabel("distance per push (m)")
        a.set_xlabel(xlabel)
        a.set_title("distance per push")
    else:
        a.text(0.5, 0.5, "no GPS", ha="center", va="center", transform=a.transAxes)

    a = ax[1, 0]
    if has_gps:
        sc = a.scatter(s["push_rate_spm"], s["speed_mps"] * 3.6, c=x, s=4, cmap="viridis")
        fig.colorbar(sc, ax=a, label=xlabel)
        a.set_xlabel("push rate (/min)")
        a.set_ylabel("speed (km/h)")
        a.set_title("speed vs push rate")
    else:
        a.hist(s["push_rate_spm"], bins=40)
        a.set_xlabel("push rate (/min)")
        a.set_title("push rate distribution")

    a = ax[1, 1]
    a.plot(x, s["contact_frac"] * 100, ".", ms=2, alpha=0.3, color="C3")
    a.plot(x, _rolling(s["contact_frac"] * 100, n), color="C3")
    a.set_ylabel("ground contact (% of cycle)")
    a.set_xlabel(xlabel)
    a.set_title("contact vs air")

    if path:
        fig.savefig(path, dpi=120)
    if show:
        plt.show()
    return fig


def plot_raw(r: Result, t0: float | None = None, span_s: float = 10.0, path=None, show: bool = False):
    """Debug view of the detection on a short window of raw signal."""
    import matplotlib.pyplot as plt

    sig = r.signals
    if t0 is None:
        t0 = float(r.strokes["t_s"].iloc[len(r.strokes) // 2]) if len(r.strokes) else float(sig.t[0])
    m = (sig.t >= t0) & (sig.t < t0 + span_s)
    t = sig.t[m]
    fig, ax = plt.subplots(3, 1, figsize=(13, 8), sharex=True, constrained_layout=True)
    acc, gyr = r.tag.accel_g[m], r.tag.gyro_dps[m]
    for k, c in enumerate("xyz"):
        ax[0].plot(t, acc[:, k], lw=0.7, label=f"a{c}")
        ax[1].plot(t, gyr[:, k], lw=0.7, label=f"g{c}")
    ax[0].set_ylabel("accel (g)")
    ax[1].plot(t, sig.gyro_lp[m], "k", lw=1.5, label="|g| low-pass")
    ax[1].set_ylabel("gyro (dps)")
    st = r.strokes[(r.strokes["t_s"] >= t0) & (r.strokes["t_s"] < t0 + span_s)]
    ax[1].plot(st["t_s"], np.interp(st["t_s"], t, sig.gyro_lp[m]), "rv", label="cycle start")
    ax[2].semilogy(t, sig.vib[m], lw=0.8, label="vibration RMS")
    ax[2].semilogy(t, sig.vib_threshold[m], "k--", lw=0.8, label="threshold")
    ax[2].set_ylabel("vibration (g)")
    ax[2].set_xlabel("time (s)")
    for a in ax:
        ylo, yhi = a.get_ylim()
        a.fill_between(t, ylo, yhi, where=sig.contact[m], color="0.85", zorder=0, step="mid")
        a.set_ylim(ylo, yhi)
        a.legend(loc="upper right", fontsize=7, ncol=4)
    ax[0].set_title("raw signal (grey: ground contact)")
    if path:
        fig.savefig(path, dpi=120)
    if show:
        plt.show()
    return fig
