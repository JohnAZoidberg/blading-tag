"""Clock fits (PROTOCOL.md 1.8).

* Sample clock: ``tag_time_us ~ t0 + sample_index * dt`` gives the true
  sample rate, which can be off from the nominal ODR by about 1 %.
* Tag -> receiver: ``elapsed_ns ~ a + b * tag_time_us``, fitted to the lower
  envelope of the receive delay, since the smallest delay is the truest.
  Packets that were buffered during a link drop arrive late and so sit far
  above the envelope; they are ignored by the fit.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .session import TagData


@dataclass(frozen=True)
class LinearMap:
    a: float
    b: float

    def __call__(self, x):
        return self.a + self.b * np.asarray(x, dtype=np.float64)


@dataclass(frozen=True)
class ClockFit:
    sample_clock: LinearMap   # sample_index -> tag_time_us
    rx_clock: LinearMap       # tag_time_us -> receiver elapsed_ns
    rate_hz: float            # true sample rate
    rx_residual_ms: float     # spread of envelope points around the fit

    def sample_elapsed_ns(self, sample_index: np.ndarray) -> np.ndarray:
        return self.rx_clock(self.sample_clock(sample_index))


def fit_sample_clock(tag: TagData) -> LinearMap:
    idx = tag.pkt_sample_index.astype(np.float64)
    ts = tag.pkt_timestamp_us.astype(np.float64)
    if len(idx) == 0:
        return LinearMap(0.0, 1e6 / tag.odr_hz)
    if len(idx) < 2 or np.ptp(idx) == 0:
        return LinearMap(ts[0] - idx[0] * 1e6 / tag.odr_hz, 1e6 / tag.odr_hz)
    # Centre for numerical stability.
    i0, t0 = idx.mean(), ts.mean()
    b = np.dot(idx - i0, ts - t0) / np.dot(idx - i0, idx - i0)
    return LinearMap(t0 - b * i0, b)


def fit_rx_clock(tag: TagData, window_s: float = 10.0, max_iter: int = 5) -> tuple[LinearMap, float]:
    ts = tag.pkt_timestamp_us.astype(np.float64)
    rx = tag.pkt_rx_ns.astype(np.float64)
    if len(ts) == 0:
        return LinearMap(0.0, 1000.0), float("nan")
    delay = rx - ts * 1000.0
    win = np.floor((ts - ts.min()) / (window_s * 1e6)).astype(np.int64)
    # Lowest delay per window.
    order = np.lexsort((delay, win))
    first = np.concatenate([[True], np.diff(win[order]) != 0])
    sel = order[first]
    x, y = ts[sel], rx[sel]
    if len(x) < 2:
        a = float(np.min(delay))
        return LinearMap(a, 1000.0), 0.0
    keep = np.ones(len(x), bool)
    for _ in range(max_iter):
        x0 = x[keep].mean()
        b, a0 = np.polyfit(x[keep] - x0, y[keep], 1)
        a = a0 - b * x0
        resid = y - (a + b * x)
        # Drop windows whose minimum was still inflated (e.g. all packets in
        # that window were from a post-reconnect burst).
        mad = np.median(np.abs(resid[keep])) + 1e5  # +0.1 ms floor
        new_keep = resid < 5 * mad
        if new_keep.sum() < 2 or np.array_equal(new_keep, keep):
            break
        keep = new_keep
    spread = float(np.std(resid[keep]) / 1e6)
    return LinearMap(float(a), float(b)), spread


def fit_clocks(tag: TagData) -> ClockFit:
    sc = fit_sample_clock(tag)
    rc, spread = fit_rx_clock(tag)
    return ClockFit(sc, rc, 1e6 / sc.b, spread)
