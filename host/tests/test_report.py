"""Per-stroke skating measures and the multi-session report, on simulated sessions."""

import json

import numpy as np
import pytest

from bladetag import report, sim, skating


@pytest.fixture(scope="module")
def two_sessions(tmp_path_factory):
    root = tmp_path_factory.mktemp("sessions")
    for seed, side in ((1, "left"), (2, "right")):
        model = sim.SkaterModel(duration_s=240, seed=seed, side=side)
        sim.write_session(root / f"session-2026010{seed}-100000", model, sim.LinkModel())
    return root


def test_stroke_table_has_every_measure(two_sessions):
    df, infos = skating.load_sessions(report.session_paths([str(two_sessions)]))
    assert [i.foot for i in infos] == ["left_foot", "right_foot"]
    assert df["session"].nunique() == 2
    m = df[df["moving"]]
    assert len(m) > 100
    for k in ("speed_kmh", "push_rate_spm", "setdown_g", "pushoff_g", "swing_dps", "road_vib_g", "lat", "bearing_deg"):
        assert m[k].notna().mean() > 0.9, k
    # The push-off kick and the set-down are both above 1 g; the kick is the harder one in the model.
    assert (m["setdown_g"] > 1).mean() > 0.9
    assert m["pushoff_g"].median() > 1


def test_correlations_and_speed_matching(two_sessions):
    df, _ = skating.load_sessions(report.session_paths([str(two_sessions)]))
    keys = skating.measures_present(df)
    c = skating.correlations(df, keys)
    for name in ("pooled", "within"):
        m = c[name]
        assert np.allclose(np.diag(m.to_numpy()), 1)
        assert np.allclose(m.to_numpy(), m.to_numpy().T, equal_nan=True)
        # Cycle period and push rate are reciprocal: rank correlation -1 (nearly,
        # within sessions, where each session's median is subtracted first).
        assert m.loc["period_s", "push_rate_spm"] == pytest.approx(-1, abs=0.01)
    at, speed = skating.at_speed(df, keys, speed_kmh=15)
    assert speed == 15 and len(at) == 2


def test_route_pairs_find_shared_ground(two_sessions):
    df, _ = skating.load_sessions(report.session_paths([str(two_sessions)]))
    # Both simulated sessions start at the same spot heading the same way.
    pairs = skating.route_pairs(df)
    assert len(pairs) and (pairs["direction"] == "same way").any()
    assert {"speed_kmh_a", "speed_kmh_b"} <= set(pairs.columns)


def test_wind_estimate():
    # No difference, no wind; a faster leg with the wind gives a positive wind.
    assert report.wind_estimate(5.0, 5.0) is None
    w = report.wind_estimate(6.0, 5.0)
    assert 0.3 < w < 3


def test_report_page(two_sessions, tmp_path):
    df, infos = skating.load_sessions(report.session_paths([str(two_sessions)]))
    rep = report.build(df, infos, "test </script> & co")
    assert rep["findings"] and all(f["text"] for f in rep["findings"])
    out = tmp_path / "r.html"
    report.write_report(out, rep)
    html = out.read_text()
    assert "/*__DATA__*/" not in html and "__TITLE__" not in html
    i = html.index('<script type="application/json" id="data">') + len('<script type="application/json" id="data">')
    data = json.loads(html[i:html.index("</script>", i)])
    assert len(data["strokes"]["session"]) == len(df)
    assert data["title"] == "test </script> & co"
    assert "test &lt;/script&gt; &amp; co" in html
