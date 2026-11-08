import base64
import json

import numpy as np
import pytest
from scipy.spatial.transform import Rotation

from bladetag import sim, viz3d
from bladetag.session import load_session


def _angle(a, b):
    return np.degrees(np.arccos(np.clip(a @ b / np.linalg.norm(a) / np.linalg.norm(b), -1, 1)))


@pytest.fixture(scope="module", params=[(0, "left"), (5, "right")], ids=["left", "right"])
def simulated(request, tmp_path_factory):
    seed, side = request.param
    folder = tmp_path_factory.mktemp(side) / "s"
    model = sim.SkaterModel(duration_s=120, side=side, seed=seed)
    sim.write_session(folder, model, sim.LinkModel(), with_gps=True)
    truth = json.loads((folder / "sim_truth.json").read_text())
    sess = load_session(folder)
    return sess, viz3d.foot_motion(sess), Rotation.from_rotvec(truth["mount_rotvec"]), side


def test_mount_found(simulated):
    _, fm, mount, side = simulated
    assert fm.side == (1 if side == "left" else -1)
    assert _angle(fm.mount.up, mount.apply([0, 0, 1])) < 3
    # The simulated skate doesn't obey the wheel constraint (its yaw isn't
    # tied to its sideways speed), so the yaw on the tag is only roughly right.
    assert _angle(fm.mount.left, mount.apply([0, 1, 0])) < 10
    assert fm.mount.agreement > 0.95


def test_stroke_path_matches_simulation(simulated):
    _, fm, _, _ = simulated
    s = fm.strokes
    assert len(s) > 50
    # The simulated foot moves 35 cm outward and back, lifts 7 cm, and doesn't move fore-aft.
    assert s["sweep_cm"].median() == pytest.approx(35, rel=0.15)
    assert s["out_cm"].median() == pytest.approx(35, rel=0.15)
    assert s["lift_cm"].median() == pytest.approx(7, abs=2)
    assert s["fore_aft_cm"].median() < 3
    # Roll swings over 35° while the wheels are down (5° to -30°).
    from scipy.spatial.transform import Rotation as R
    edge, _, _ = viz3d._angles(R.from_quat(fm.quat).as_matrix(), fm.side)
    c = fm.contact & (fm.stroke >= 0)
    assert np.percentile(edge[c], 97) - np.percentile(edge[c], 3) == pytest.approx(35, abs=6)


def test_stroke_motion_merges_with_analysis(simulated):
    sess, fm, _, _ = simulated
    df = viz3d.stroke_motion(sess, fm.result)
    st = fm.result.strokes
    assert np.allclose(st.loc[df["stroke"], "t_s"].to_numpy(), df["t_s"].to_numpy())


def test_page_embeds_decodable_data(simulated, tmp_path):
    _, fm, _, _ = simulated
    out = viz3d.write_html([fm], tmp_path / "p.html")
    html = out.read_text()
    assert "/*__DATA__*/" not in html
    start = html.index("const DATA = ") + len("const DATA = ")
    end = html.index(";\n(function", start)
    data = json.loads(html[start:end])
    s = data["sessions"][0]
    n = s["n"]
    assert len(base64.b64decode(s["pos_mm"])) == n * 3 * 2
    assert len(base64.b64decode(s["quat"])) == n * 4 * 2
    assert len(s["strokes"]["t_s"]) == len(fm.strokes)
    assert set(s["typical"]) == {"all", "early", "late"}


def test_wheels_stay_on_or_above_the_road(simulated):
    _, fm, _, _ = simulated
    r = Rotation.from_quat(fm.quat).as_matrix()
    org = viz3d.model_origin(fm.pos, r, fm.contact)
    reach = viz3d.WHEEL_R * np.sqrt(1 - r[:, 2, 1] ** 2)
    lowest = org[:, 2] + np.min([r[:, 2, :] @ [x, 0, viz3d.WHEEL_R] for x in viz3d.WHEEL_X], axis=0) - reach
    assert lowest.min() > -1e-6
    assert np.abs(lowest[fm.contact]).max() < 1e-6
    # Skate level front to back while the wheels are down.
    _, _, pitch = viz3d._angles(r, fm.side)
    c = fm.contact & (fm.stroke >= 0)
    assert np.percentile(np.abs(pitch[c]), 90) < 3
