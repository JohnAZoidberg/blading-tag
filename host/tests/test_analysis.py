import json

import numpy as np
import pytest

from bladetag import sim
from bladetag.analysis import analyze, format_summary
from bladetag.session import load_session


def simulate(tmp_path, **kw):
    gps = kw.pop("with_gps", True)
    model = sim.SkaterModel(duration_s=kw.pop("duration_s", 300), **kw)
    sim.write_session(tmp_path / "s", model, sim.LinkModel(drops=[(100.0, 5.0)]), with_gps=gps)
    truth = json.loads((tmp_path / "s" / "sim_truth.json").read_text())
    return load_session(tmp_path / "s"), model, truth


def check_cycles(r, model, truth):
    s = r.strokes
    n_true = len(truth["set_down_s"])
    assert len(s) >= 0.95 * n_true - 2       # a few cycles at starts/stops get dropped
    assert len(s) <= n_true
    true_period = (model.cycle_s_start + model.cycle_s_end) / 2
    assert s["period_s"].median() == pytest.approx(true_period, rel=0.03)
    assert s["contact_frac"].median() == pytest.approx(model.contact_end, abs=0.03)


@pytest.mark.parametrize("seed,side", [(0, "left"), (5, "right")])
def test_cycles_match_truth(tmp_path, seed, side):
    sess, model, truth = simulate(tmp_path, seed=seed, side=side)
    r = analyze(sess)
    assert r.summary["cycle_method"] == "setdown"
    check_cycles(r, model, truth)
    # Set-down times line up with the truth to within a few samples.
    off = r.signals.t[0]
    true_sd = np.array(truth["set_down_s"])
    err = [np.min(np.abs(true_sd - (x - off))) for x in r.strokes["set_down_s"].dropna()]
    assert np.median(err) < 0.02


def test_gyro_method(tmp_path):
    sess, model, truth = simulate(tmp_path)
    r = analyze(sess, method="gyro")
    check_cycles(r, model, truth)


def test_fatigue_drift_and_distance(tmp_path):
    sess, model, truth = simulate(tmp_path, duration_s=600)
    r = analyze(sess)
    d = r.summary["drift"]
    assert d["push_rate_spm"]["change_pct"] < -4       # slower cadence late in the session
    assert d["speed_mps"]["change_pct"] < -4
    # dist/push = speed * period / 2
    expected = (model.speed_start_mps * model.cycle_s_start) / 2
    assert d["dist_per_push_m"]["first"] == pytest.approx(expected, rel=0.05)
    assert r.summary["distance_km"] == pytest.approx(4.1, rel=0.1)
    assert r.per_km is not None and len(r.per_km) >= 4
    assert "fatigue drift" in format_summary(r)


def test_no_gps(tmp_path):
    sess, model, truth = simulate(tmp_path, with_gps=False)
    r = analyze(sess)
    assert r.gps is None and r.per_km is None
    assert any("no GPS" in w for w in r.warnings)
    check_cycles(r, model, truth)


def test_weak_vibration_falls_back_to_gyro(tmp_path):
    sess, model, truth = simulate(tmp_path, vibration_g=0.0)
    r = analyze(sess)
    assert r.summary["cycle_method"] == "gyro"
    assert any("vibration" in w for w in r.warnings)
    true_period = (model.cycle_s_start + model.cycle_s_end) / 2
    assert r.strokes["period_s"].median() == pytest.approx(true_period, rel=0.03)
