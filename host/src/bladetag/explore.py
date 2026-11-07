"""Tools for looking at a real recording before deciding how to analyse it.

Nothing here detects strokes. It shows the signal so you can see what to
detect:

* ``sample_frame``: every sample on the session time base, in g and dps, as
  a DataFrame. Use it in a notebook (``load``) or write it out with
  ``bladetag explore --export``.
* ``plot_overview``: the whole session on one zoomable figure: accel and
  gyro axes, an acceleration spectrogram, a cadence track and GPS speed.
  Zoomed out the axes are drawn as a min/max envelope, zoomed in as the raw
  samples.
* ``plot_psd``: spectra of chosen time ranges, to compare e.g. walking with
  skating, smooth with rough asphalt, or a frame with a cuff mount.
* ``describe``: a text summary: rest periods and which sensor axis points
  up, clipping, cadence, and how much acceleration sits in the road
  vibration band.

Times are seconds since the session start, the same as ``bladetag analyze
--raw-at``.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
from scipy import signal

from .session import Session, TagData, load_session
from .timesync import ClockFit, fit_clocks

ACCEL = ["ax", "ay", "az"]
GYRO = ["gx", "gy", "gz"]
ACTIVE_GYRO_DPS = 30.0   # 1 s mean |gyro| above this counts as moving (same as analysis.Params)
VIB_HP_HZ = 20.0         # road vibration band (same as analysis.Params)


# --- data --------------------------------------------------------------------


def sample_frame(sess: Session, tag: TagData, clock: ClockFit | None = None) -> pd.DataFrame:
    """One row per sample.

    Columns: t_s (seconds since session start, on the receiver clock),
    sample_index, segment (increments at every gap), ax/ay/az (g),
    gx/gy/gz (dps), a_mag, g_mag, a_clip/g_clip (an axis at the end of its
    range).
    """
    if len(tag.sample_index) == 0:
        raise ValueError("tag log has no IMU samples")
    clock = clock or fit_clocks(tag)
    idx = tag.sample_index
    acc, gyr = tag.accel_g, tag.gyro_dps
    raw = np.abs(tag.raw.astype(np.int32))
    df = pd.DataFrame({
        "t_s": (clock.sample_elapsed_ns(idx) - sess.start_elapsed_ns) / 1e9,
        "sample_index": idx,
        "segment": np.cumsum(np.concatenate([[0], np.diff(idx) != 1])),
    })
    df[ACCEL] = acc
    df[GYRO] = gyr
    df["a_mag"] = np.linalg.norm(acc, axis=1)
    df["g_mag"] = np.linalg.norm(gyr, axis=1)
    df["a_clip"] = (raw[:, :3] >= 32767).any(axis=1)
    df["g_clip"] = (raw[:, 3:] >= 32767).any(axis=1)
    return df


def gps_frame(sess: Session) -> pd.DataFrame | None:
    from .analysis import gps_frame as _gps

    return _gps(sess)


def load(path: str | Path, tag: str | None = None) -> tuple[pd.DataFrame, float, pd.DataFrame | None]:
    """Notebook helper: (samples, true sample rate in Hz, GPS or None).

    ``tag`` is "left", "right" or an address; the first tag by default.
    """
    sess = load_session(path)
    t = sess.tags[0]
    if tag:
        t = next(x for x in sess.tags if tag in (x.meta.position, x.meta.address))
    clock = fit_clocks(t)
    return sample_frame(sess, t, clock), clock.rate_hz, gps_frame(sess)


def crop(df: pd.DataFrame, start: float | None = None, end: float | None = None) -> pd.DataFrame:
    m = np.ones(len(df), bool)
    if start is not None:
        m &= df["t_s"].to_numpy() >= start
    if end is not None:
        m &= df["t_s"].to_numpy() < end
    out = df[m].reset_index(drop=True)
    if out.empty:
        raise ValueError(f"no samples between {start} and {end} s")
    return out


def export(df: pd.DataFrame, path: str | Path) -> None:
    path = Path(path)
    if path.suffix == ".csv":
        df.to_csv(path, index=False, float_format="%.6g")
    elif path.suffix == ".parquet":
        df.to_parquet(path, index=False)  # needs pyarrow
    else:
        raise ValueError("export to a .csv or .parquet file")


# --- derived signals ---------------------------------------------------------


def _runs(mask: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Start and end (exclusive) indices of the True runs."""
    d = np.diff(np.concatenate([[0], mask.astype(np.int8), [0]]))
    return np.flatnonzero(d == 1), np.flatnonzero(d == -1)


def still_mask(df: pd.DataFrame, fs: float, win_s: float = 1.0, max_acc_std_g: float = 0.03,
               max_gyro_dps: float = 8.0) -> np.ndarray:
    """True where the tag is at rest: |a| steady and hardly any rotation."""
    n = max(2, int(win_s * fs))
    a = df["a_mag"].rolling(n, center=True, min_periods=n // 2).std()
    g = df["g_mag"].rolling(n, center=True, min_periods=n // 2).mean()
    return ((a < max_acc_std_g) & (g < max_gyro_dps)).to_numpy()


def active_mask(df: pd.DataFrame, fs: float) -> np.ndarray:
    n = max(1, int(fs))
    return (df["g_mag"].rolling(n, center=True, min_periods=n // 2).mean() > ACTIVE_GYRO_DPS).to_numpy()


def cadence_track(df: pd.DataFrame, fs: float, win_s: float = 8.0, step_s: float = 1.0,
                  min_period_s: float = 0.3, max_period_s: float = 3.0) -> pd.DataFrame:
    """Dominant repetition period in sliding windows, from the autocorrelation
    of the gyro vector (summed over axes, so it doesn't depend on the mount
    and has no |x| rectification doubling the frequency).

    Columns: t_s (window centre), period_s, strength (autocorrelation at that
    lag, 0..1: how repetitive the window is), gyro_rms_dps.
    """
    g = df[GYRO].to_numpy()
    t = df["t_s"].to_numpy()
    seg = df["segment"].to_numpy()
    n = int(win_s * fs)
    step = max(1, int(step_s * fs))
    lo = max(1, int(min_period_s * fs))
    hi = min(int(max_period_s * fs), n // 2)
    nfft = 1 << int(np.ceil(np.log2(2 * n)))
    rows = []
    bounds = np.flatnonzero(np.diff(seg) != 0) + 1
    for a, b in zip(np.concatenate([[0], bounds]), np.concatenate([bounds, [len(seg)]])):
        for i0 in range(a, b - n + 1, step):
            x = g[i0:i0 + n]
            x = x - x.mean(axis=0)
            f = np.fft.rfft(x, nfft, axis=0)
            ac = np.fft.irfft(np.abs(f) ** 2, nfft, axis=0)[:n].sum(axis=1)
            if ac[0] <= 0:
                continue
            ac = ac / ac[0]
            pk, _ = signal.find_peaks(ac[lo:hi + 1])
            if not len(pk):
                continue
            vals = ac[lo + pk]
            # Multiples of the period also correlate; take the first peak
            # that is nearly as strong as the best one.
            j = pk[int(np.argmax(vals >= 0.8 * vals.max()))]
            rows.append((t[i0 + n // 2], (lo + j) / fs, float(ac[lo + j]),
                         float(np.sqrt((x ** 2).sum(axis=1).mean()))))
    return pd.DataFrame(rows, columns=["t_s", "period_s", "strength", "gyro_rms_dps"])


def spectrogram(df: pd.DataFrame, fs: float, cols=ACCEL, nperseg: int = 256, max_cols: int = 3000):
    """Per contiguous segment: (t_s, f_hz, power). Power is summed over
    ``cols`` (each detrended), so it's independent of the mount. Long
    sessions are averaged down to about ``max_cols`` time columns."""
    hop = nperseg // 2
    factor = max(1, int(np.ceil(len(df) / hop / max_cols)))
    t = df["t_s"].to_numpy()
    seg = df["segment"].to_numpy()
    x = df[list(cols)].to_numpy()
    out = []
    bounds = np.flatnonzero(np.diff(seg) != 0) + 1
    for a, b in zip(np.concatenate([[0], bounds]), np.concatenate([bounds, [len(seg)]])):
        if b - a < nperseg:
            continue
        f, tt, s = signal.spectrogram(x[a:b], fs, nperseg=nperseg, noverlap=nperseg - hop,
                                      detrend="constant", axis=0)
        s = s.sum(axis=1)  # (f, axis, time) -> (f, time)
        if factor > 1 and s.shape[1] >= factor:
            m = s.shape[1] // factor * factor
            s = s[:, :m].reshape(len(f), -1, factor).mean(axis=2)
            tt = tt[:m].reshape(-1, factor).mean(axis=1)
        out.append((t[a] + tt, f, s))
    return out


def band_rms(df: pd.DataFrame, fs: float, hp_hz: float = VIB_HP_HZ) -> np.ndarray:
    """Per-sample magnitude of the high-passed acceleration vector (g)."""
    sos = signal.butter(4, hp_hz, "highpass", fs=fs, output="sos")
    x = df[ACCEL].to_numpy()
    seg = df["segment"].to_numpy()
    out = np.full(len(df), np.nan)
    pad = 3 * (2 * len(sos) + 1)
    bounds = np.flatnonzero(np.diff(seg) != 0) + 1
    for a, b in zip(np.concatenate([[0], bounds]), np.concatenate([bounds, [len(seg)]])):
        if b - a > pad + 1:
            out[a:b] = np.linalg.norm(signal.sosfiltfilt(sos, x[a:b], axis=0), axis=1)
    return out


# --- text summary ------------------------------------------------------------


def _up_axis(v: np.ndarray) -> str:
    k = int(np.argmax(np.abs(v)))
    return ("+" if v[k] > 0 else "-") + "xyz"[k]


def describe(df: pd.DataFrame, fs: float, accel_range_g: int, gyro_range_dps: int,
             gps: pd.DataFrame | None = None, cad: pd.DataFrame | None = None,
             still: np.ndarray | None = None) -> list[str]:
    t = df["t_s"].to_numpy()
    still = still_mask(df, fs) if still is None else still
    active = active_mask(df, fs)
    cad = cadence_track(df, fs) if cad is None else cad
    lines = [f"  span            {t[0]:.1f} to {t[-1]:.1f} s ({t[-1] - t[0]:.1f} s), {fs:.2f} Hz,"
             f" {df['segment'].nunique() - 1} gap(s)",
             f"  moving          {active.sum() / fs:.1f} s (1 s mean |gyro| > {ACTIVE_GYRO_DPS:.0f} dps)"]

    rs, re = _runs(still)
    if len(rs):
        lines.append(f"  still           {still.sum() / fs:.1f} s in {len(rs)} period(s). At rest the"
                     " accelerometer reads +1 g pointing up:")
        longest = np.argsort(re - rs)[::-1][:4]
        for i in sorted(longest):
            v = df.loc[rs[i]:re[i] - 1, ACCEL].mean().to_numpy()
            lines.append(f"    {t[rs[i]]:7.1f}-{t[re[i] - 1]:7.1f} s  a = ({v[0]:+.2f}, {v[1]:+.2f}, {v[2]:+.2f}) g,"
                         f" |a| {np.linalg.norm(v):.3f}, up = {_up_axis(v)}")
    else:
        lines.append("  still           never (no rest period to find gravity from)")

    a, g = df["a_mag"], df["g_mag"]
    lines.append(f"  accel |a|       median {a.median():.2f} g, 99th pct {a.quantile(0.99):.2f} g,"
                 f" max {a.max():.2f} g; {int(df['a_clip'].sum())} samples at the ±{accel_range_g} g limit")
    lines.append(f"  gyro |g|        median {g.median():.0f} dps, 99th pct {g.quantile(0.99):.0f} dps,"
                 f" max {g.max():.0f} dps; {int(df['g_clip'].sum())} samples at the ±{gyro_range_dps} dps limit")

    vib = band_rms(df, fs)
    parts = []
    for name, m in (("moving", active), ("still", still)):
        if m.any():
            parts.append(f"{np.sqrt(np.nanmean(vib[m] ** 2)):.3f} g {name}")
    if parts:
        lines.append(f"  accel > {VIB_HP_HZ:.0f} Hz    RMS " + ", ".join(parts))

    if len(cad):
        act = cad[cad["gyro_rms_dps"] > ACTIVE_GYRO_DPS]
        rep = act[act["strength"] > 0.5]
        if len(act) and len(rep):
            q1, med, q3 = rep["period_s"].quantile([0.25, 0.5, 0.75])
            lines.append(f"  cadence         period {med:.2f} s median ({q1:.2f}-{q3:.2f}), {60 / med:.0f} cycles/min;"
                         f" repetitive in {100 * len(rep) / len(act):.0f} % of moving windows")
        elif len(act):
            lines.append("  cadence         no repetitive motion found while moving")

    if gps is not None and len(gps):
        sp = gps["speed_mps"]
        h = gps["h_acc_m"]
        lines.append(f"  gps             {len(gps)} fixes, speed median {sp.median() * 3.6:.1f} km/h,"
                     f" max {sp.max() * 3.6:.1f} km/h; accuracy {h.min():.0f}-{h.max():.0f} m")
    return lines


# --- plots -------------------------------------------------------------------


def _with_gaps(t: np.ndarray, seg: np.ndarray, ys: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Insert a NaN row at every gap so lines break there."""
    brk = np.flatnonzero(np.diff(seg) != 0) + 1
    if not len(brk):
        return t.astype(float), ys.astype(float)
    tt = np.insert(t.astype(float), brk, (t[brk - 1] + t[brk]) / 2)
    yy = np.insert(ys.astype(float), brk, np.nan, axis=0)
    return tt, yy


class EnvelopeLines:
    """Lines that show the raw samples when zoomed in and a min/max envelope
    when zoomed out, so a two-hour session stays responsive."""

    def __init__(self, ax, t: np.ndarray, ys: np.ndarray, styles: list[dict], max_bins: int = 2500):
        self.ax, self.t, self.ys, self.max_bins = ax, t, ys, max_bins
        self.lines = [ax.plot([], [], **st)[0] for st in styles]
        ax.callbacks.connect("xlim_changed", self.update)

    def update(self, ax=None):
        lo, hi = self.ax.get_xlim()
        i0 = max(0, int(np.searchsorted(self.t, lo)) - 1)
        i1 = min(len(self.t), int(np.searchsorted(self.t, hi)) + 1)
        n = i1 - i0
        if n <= 2 * self.max_bins:
            for k, line in enumerate(self.lines):
                line.set_data(self.t[i0:i1], self.ys[i0:i1, k])
            return
        b = int(np.ceil(n / self.max_bins))
        starts = np.arange(i0, i1, b)
        mids = np.minimum(starts + b // 2, i1 - 1)
        tt = np.empty(2 * len(starts))
        tt[0::2], tt[1::2] = self.t[starts], self.t[mids]
        for k, line in enumerate(self.lines):
            y = self.ys[i0:i1, k]
            yy = np.empty(2 * len(starts))
            # fmin/fmax skip NaN unless the whole bin is a gap.
            yy[0::2] = np.fmin.reduceat(y, starts - i0)
            yy[1::2] = np.fmax.reduceat(y, starts - i0)
            line.set_data(tt, yy)


def _shade(axs, spans, **kw):
    for a, b in spans:
        for ax in axs:
            ax.axvspan(a, b, lw=0, **kw)


def plot_overview(df: pd.DataFrame, fs: float, gps: pd.DataFrame | None = None, title: str = "",
                  cad: pd.DataFrame | None = None, still: np.ndarray | None = None, path=None):
    """The whole (cropped) session on shared, zoomable time axes."""
    import matplotlib.pyplot as plt

    t = df["t_s"].to_numpy()
    seg = df["segment"].to_numpy()
    still = still_mask(df, fs) if still is None else still
    cad = cadence_track(df, fs) if cad is None else cad
    rows = ["accel", "gyro", "spec", "cadence"] + (["gps"] if gps is not None and len(gps) else [])
    ratios = {"accel": 3, "gyro": 3, "spec": 2.5, "cadence": 1.5, "gps": 1.5}
    fig, axs = plt.subplots(len(rows), 1, sharex=True, figsize=(14, 2.2 * len(rows)),
                            height_ratios=[ratios[r] for r in rows], constrained_layout=True)
    ax = dict(zip(rows, axs))
    fig.suptitle(title + "   (grey: at rest, red: gap)")
    keep = []

    for name, cols, unit in (("accel", ACCEL, "g"), ("gyro", GYRO, "dps")):
        tt, yy = _with_gaps(t, seg, df[cols].to_numpy())
        el = EnvelopeLines(ax[name], tt, yy, [dict(lw=0.6, color=f"C{k}", label=c) for k, c in enumerate(cols)])
        keep.append(el)
        lo, hi = np.nanmin(yy), np.nanmax(yy)
        pad = 0.05 * (hi - lo or 1)
        ax[name].set_ylim(lo - pad, hi + pad)
        ax[name].set_ylabel(f"{name} ({unit})")
        ax[name].legend(loc="upper right", fontsize=7, ncol=3)

    a = ax["spec"]
    specs = spectrogram(df, fs)
    if specs:
        allp = np.concatenate([s[2].ravel() for s in specs])
        db = 10 * np.log10(allp + 1e-12)
        vmin, vmax = np.percentile(db, [5, 99.5])
        for tt, f, s in specs:
            a.pcolormesh(tt, f, 10 * np.log10(s + 1e-12), shading="nearest", vmin=vmin, vmax=vmax,
                         cmap="magma", rasterized=True)
        a.axhline(VIB_HP_HZ, color="w", lw=0.6, ls="--")
    a.set_ylabel("accel spectrum\n(Hz)")

    a = ax["cadence"]
    if len(cad):
        c = cad[cad["gyro_rms_dps"] > ACTIVE_GYRO_DPS]
        sc = a.scatter(c["t_s"], 60 / c["period_s"], c=c["strength"], s=6, cmap="viridis", vmin=0, vmax=1)
        fig.colorbar(sc, ax=a, label="repetitive", pad=0.005, aspect=10)
    a.set_ylabel("cycles/min\n(gyro autocorr)")

    if "gps" in ax:
        a = ax["gps"]
        a.plot(gps["t_s"], gps["speed_mps"] * 3.6, ".-", ms=3, lw=0.8, color="C1")
        a.set_ylabel("GPS speed\n(km/h)")

    rs, re = _runs(still)
    _shade(axs, [(t[i], t[j - 1]) for i, j in zip(rs, re)], color="0.88", zorder=0)
    brk = np.flatnonzero(np.diff(seg) != 0) + 1
    _shade(axs, [(t[i - 1], t[i]) for i in brk], color="red", alpha=0.25, zorder=0)

    axs[-1].set_xlabel("time since session start (s)")
    axs[-1].set_xlim(t[0], t[-1])
    for el in keep:
        el.update()
    fig._bladetag_keep = keep  # callbacks only hold weak references
    if path:
        fig.savefig(path, dpi=110)
    return fig


def parse_range(s: str) -> tuple[float, float, str]:
    """'START:END' or 'START:END:label' in seconds."""
    parts = s.split(":", 2)
    if len(parts) < 2:
        raise ValueError(f"expected START:END[:label], got {s!r}")
    a, b = float(parts[0]), float(parts[1])
    return a, b, parts[2] if len(parts) > 2 else f"{a:g}-{b:g} s"


def psd(df: pd.DataFrame, fs: float, start: float, end: float, cols, seg_s: float = 10.0):
    """Welch spectrum summed over ``cols``, averaged over the contiguous
    pieces of [start, end). Welch segments are ``seg_s`` long (0.1 Hz
    resolution, enough to separate stroke cadences), or shorter when the
    range is. Returns (f, power) or None if too short."""
    t = df["t_s"].to_numpy()
    seg = df["segment"].to_numpy()
    m = (t >= start) & (t < end)
    pieces = [df.loc[m & (seg == s), list(cols)].to_numpy() for s in np.unique(seg[m])]
    longest = max((len(x) for x in pieces), default=0)
    nperseg = min(int(seg_s * fs), longest)
    if nperseg < 64:
        return None
    total, weight, f = None, 0, None
    for x in pieces:
        if len(x) < nperseg:
            continue
        f, pw = signal.welch(x, fs, nperseg=nperseg, axis=0, detrend="constant")
        pw = pw.sum(axis=1)
        total = pw * len(x) if total is None else total + pw * len(x)
        weight += len(x)
    return (f, total / weight) if weight else None


def plot_psd(df: pd.DataFrame, fs: float, ranges: list[tuple[float, float, str]], title: str = "", path=None):
    import matplotlib.pyplot as plt

    fig, axs = plt.subplots(1, 2, figsize=(14, 5), constrained_layout=True)
    fig.suptitle(title)
    for k, (a, b, label) in enumerate(ranges):
        for ax, cols in zip(axs, (ACCEL, GYRO)):
            r = psd(df, fs, a, b, cols)
            if r is None:
                continue
            f, pw = r
            ax.loglog(f[1:], pw[1:], lw=1, color=f"C{k}", label=label)
    axs[0].set_ylabel("accel PSD, sum of axes (g²/Hz)")
    axs[1].set_ylabel("gyro PSD, sum of axes (dps²/Hz)")
    for ax in axs:
        ax.set_xlabel("frequency (Hz)")
        ax.axvline(VIB_HP_HZ, color="0.5", lw=0.6, ls="--")
        ax.grid(True, which="both", alpha=0.3)
        if ax.has_data():
            ax.legend(fontsize=8)
    if path:
        fig.savefig(path, dpi=110)
    return fig
