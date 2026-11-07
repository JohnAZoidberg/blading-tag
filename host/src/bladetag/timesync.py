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
    rx_residual_ms: float     # spread of envelope points around the fit; nan if too short to tell

    def sample_elapsed_ns(self, sample_index: np.ndarray) -> np.ndarray:
        return self.rx_clock(self.sample_clock(sample_index))

    def tag_us_to_elapsed_ns(self, tag_us) -> np.ndarray:
        return self.rx_clock(tag_us)


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


def fit_rx_clock(tag: TagData, sample_clock: LinearMap | None = None, window_s: float = 10.0,
                 max_iter: int = 5) -> tuple[LinearMap, float]:
    sample_clock = sample_clock or fit_sample_clock(tag)
    # A notification goes out once its *last* sample is taken, so short
    # packets (e.g. the STOP flush) would bias the envelope if we used the
    # header timestamp. Use the last sample's time instead.
    ts = tag.pkt_timestamp_us + sample_clock.b * np.maximum(tag.pkt_n - 1, 0)
    rx = tag.pkt_rx_ns.astype(np.float64)
    if len(ts) == 0:
        return LinearMap(0.0, 1000.0), float("nan")
    live = _live_packets(tag)
    if np.count_nonzero(live) >= 2:
        ts, rx = ts[live], rx[live]
    delay = rx - ts * 1000.0
    win = np.floor((ts - ts.min()) / (window_s * 1e6)).astype(np.int64)
    # Lowest delay per window.
    order = np.lexsort((delay, win))
    first = np.concatenate([[True], np.diff(win[order]) != 0])
    sel = order[first]
    x, y = ts[sel], rx[sel]
    if len(x) < 2:
        a = float(np.min(delay))
        return LinearMap(a, 1000.0), float("nan")
    keep = np.ones(len(x), bool)
    for _ in range(max_iter):
        x0 = x[keep].mean()
        b, a0 = np.polyfit(x[keep] - x0, y[keep], 1)
        a = a0 - b * x0
        resid = y - (a + b * x)
        # Drop windows whose minimum was still inflated (e.g. all packets in
        # that window were from a post-reconnect burst).
        mad = np.median(np.abs(resid[keep])) + 1e5  # +0.1 ms floor
        new_keep = np.abs(resid) < 5 * mad
        if new_keep.sum() < 2 or np.array_equal(new_keep, keep):
            break
        keep = new_keep
    # Two windows always fit exactly, so the spread says nothing until there
    # are three (a recording of 20 s or more).
    spread = float(np.std(resid[keep]) / 1e6) if keep.sum() >= 3 else float("nan")
    return LinearMap(float(a), float(b)), spread


def _live_packets(tag: TagData, backlog_s: float = 0.5) -> np.ndarray:
    """Packets that weren't sent from a backlog. When a recorder joins a
    running recording, or right after a reconnect, the tag first drains its
    buffer: those packets arrive late, and if they make up a large share of
    the log they pull the envelope fit off. The logged Status records say how
    full the buffer was, so drop packets received while it held more than
    ``backlog_s`` of samples."""
    n = len(tag.pkt_rx_ns)
    if not tag.statuses:
        return np.ones(n, bool)
    st_rx = np.array([t for t, _ in tag.statuses], dtype=np.int64)
    fill = np.array([s.buffer_fill for _, s in tag.statuses], dtype=np.int64)
    order = np.argsort(st_rx, kind="stable")
    st_rx, fill = st_rx[order], fill[order]
    # The Status in effect when each packet arrived. Packets that arrived
    # before the first Status use that one (it is read right after subscribing).
    i = np.clip(np.searchsorted(st_rx, tag.pkt_rx_ns, side="right") - 1, 0, len(st_rx) - 1)
    return fill[i] <= backlog_s * tag.odr_hz


def fit_clocks(tag: TagData) -> ClockFit:
    sc = fit_sample_clock(tag)
    rc, spread = fit_rx_clock(tag, sc)
    return ClockFit(sc, rc, 1e6 / sc.b, spread)
