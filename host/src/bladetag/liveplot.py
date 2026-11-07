"""Live plot while recording. The recorder runs on an asyncio loop in a
background thread; matplotlib owns the main thread."""

from __future__ import annotations

import threading

import numpy as np

from . import protocol as p


class Ring:
    def __init__(self, seconds: float = 10.0, rate: int = 200):
        self.n = int(seconds * rate)
        self.rate = rate
        self.buf = np.zeros((self.n, 6), np.float32)
        self.count = 0
        self.lock = threading.Lock()
        self.status: p.Status | None = None

    def push(self, pkt: p.DataPacket, status: p.Status | None) -> None:
        acc_s = p.accel_scale_g(status.accel_range_g if status else p.DEFAULT_ACCEL_RANGE_G)
        gyr_s = p.gyro_scale_dps(status.gyro_range_dps if status else p.DEFAULT_GYRO_RANGE_DPS)
        x = pkt.samples.astype(np.float32)
        x[:, :3] *= acc_s
        x[:, 3:] *= gyr_s
        with self.lock:
            k = len(x)
            self.buf = np.roll(self.buf, -k, axis=0)
            self.buf[-k:] = x[-self.n:]
            self.count += k
            self.status = status

    def snapshot(self):
        with self.lock:
            return self.buf.copy(), self.count, self.status


def run_live_plot(ring: Ring, title: str, done: threading.Event) -> None:
    import matplotlib.pyplot as plt
    from matplotlib.animation import FuncAnimation

    t = (np.arange(ring.n) - ring.n) / ring.rate
    fig, (a1, a2) = plt.subplots(2, 1, sharex=True, figsize=(11, 6), constrained_layout=True)
    fig.canvas.manager.set_window_title(title)
    la = [a1.plot(t, np.zeros(ring.n), lw=0.7, label=c)[0] for c in ("ax", "ay", "az")]
    lg = [a2.plot(t, np.zeros(ring.n), lw=0.7, label=c)[0] for c in ("gx", "gy", "gz")]
    a1.set_ylabel("accel (g)")
    a2.set_ylabel("gyro (dps)")
    a2.set_xlabel("time (s)")
    a1.legend(loc="upper left", ncol=3)
    a2.legend(loc="upper left", ncol=3)
    txt = a1.text(0.99, 0.97, "", transform=a1.transAxes, ha="right", va="top", fontsize=8, family="monospace")

    def update(_):
        if done.is_set():
            plt.close(fig)
            return []
        buf, count, st = ring.snapshot()
        for k, line in enumerate(la):
            line.set_ydata(buf[:, k])
        for k, line in enumerate(lg):
            line.set_ydata(buf[:, 3 + k])
        a = max(1.5, float(np.abs(buf[:, :3]).max()) * 1.1)
        g = max(50.0, float(np.abs(buf[:, 3:]).max()) * 1.1)
        a1.set_ylim(-a, a)
        a2.set_ylim(-g, g)
        if st:
            txt.set_text(f"{st.state.name}  {st.battery_mv} mV  rec {st.samples_recorded}  "
                         f"drop {st.samples_dropped}  buf {st.buffer_fill}")
        return la + lg + [txt]

    anim = FuncAnimation(fig, update, interval=100, cache_frame_data=False)  # noqa: F841 - keep a ref
    plt.show()
