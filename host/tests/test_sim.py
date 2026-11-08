import json

import numpy as np
import pytest

from bladetag import sim
from bladetag.session import load_session
from bladetag.timesync import fit_clocks


def test_sim_session_roundtrip(tmp_path):
    model = sim.SkaterModel(duration_s=120, pause=None)
    link = sim.LinkModel(drops=[(30.0, 5.0)])
    sim.write_session(tmp_path / "s", model, link)
    s = load_session(tmp_path / "s")
    t = s.tags[0]
    assert t.meta.position == "left_foot"
    assert t.meta.simulated and s.simulated
    assert len(t.raw) == int(120 * link.true_rate_hz)  # short drop: nothing lost
    assert t.gaps() == [] and t.seq_gaps() == 0
    fit = fit_clocks(t)
    assert fit.rate_hz == pytest.approx(link.true_rate_hz, rel=1e-6)
    assert fit.rx_clock.b == pytest.approx(1000 * link.clock_skew, rel=2e-6)
    # Standing still at the start: |a| = 1 g.
    assert np.linalg.norm(t.accel_g[:400], axis=1).mean() == pytest.approx(1.0, abs=0.01)
    assert s.gps is not None and abs(len(s.gps) - 120) <= 1


def test_sim_long_drop_overflows_buffer(tmp_path):
    model = sim.SkaterModel(duration_s=120, pause=None)
    link = sim.LinkModel(drops=[(30.0, 50.0)], buffer_capacity=8000)
    sim.write_session(tmp_path / "s", model, link)
    t = load_session(tmp_path / "s").tags[0]
    truth = json.loads((tmp_path / "s" / "sim_truth.json").read_text())
    (start, missing), = t.gaps()
    assert missing == truth["samples_dropped"] > 0
    assert t.statuses[-1][1].samples_dropped == missing
