import matplotlib

matplotlib.use("Agg")

import numpy as np
import pandas as pd
import pytest

from bladetag import explore as ex
from bladetag import sim
from bladetag.cli import main
from bladetag.session import load_session


@pytest.fixture(scope="module")
def session(tmp_path_factory):
    path = tmp_path_factory.mktemp("explore") / "s"
    model = sim.SkaterModel(duration_s=240, seed=1)
    sim.write_session(path, model, sim.LinkModel(drops=[(100.0, 40.0)]), with_gps=True)
    return path, model


def test_sample_frame(session):
    path, _ = session
    df, fs, gps = ex.load(path)
    assert list(df.columns[:9]) == ["t_s", "sample_index", "segment", *ex.ACCEL, *ex.GYRO]
    assert np.all(np.diff(df["t_s"]) > 0)
    assert df["segment"].nunique() == 2       # the 40 s drop overflows the buffer
    assert fs == pytest.approx(200, rel=0.02)
    assert gps is not None and len(gps)


def test_still_and_gravity(session):
    path, _ = session
    df, fs, _ = ex.load(path)
    still = ex.still_mask(df, fs)
    # The model stands still for the first 3 s.
    assert still[: int(2 * fs)].mean() > 0.5
    assert np.linalg.norm(df.loc[still, ex.ACCEL].mean()) == pytest.approx(1.0, abs=0.02)


def test_cadence_track_finds_stroke_period(session):
    path, model = session
    df, fs, _ = ex.load(path)
    cad = ex.cadence_track(df, fs)
    moving = cad[(cad["gyro_rms_dps"] > ex.ACTIVE_GYRO_DPS) & (cad["strength"] > 0.5)]
    assert len(moving) > 100
    lo, hi = model.cycle_s_start, model.cycle_s_end
    assert lo * 0.97 < moving["period_s"].median() < hi * 1.03
    # No 8 s window spans the gap (where the buffer overflowed).
    b = np.flatnonzero(np.diff(df["segment"]))[0]
    t0, t1 = df["t_s"][b], df["t_s"][b + 1]
    assert not ((cad["t_s"] > t0 - 3.9) & (cad["t_s"] < t1 + 3.9)).any()


def test_envelope_switches_to_raw_when_zoomed(session):
    import matplotlib.pyplot as plt

    path, _ = session
    df, fs, _ = ex.load(path)
    fig, ax = plt.subplots()
    t, y = ex._with_gaps(df["t_s"].to_numpy(), df["segment"].to_numpy(), df[ex.ACCEL].to_numpy())
    el = ex.EnvelopeLines(ax, t, y, [{}, {}, {}], max_bins=500)
    ax.set_xlim(t[0], t[-1])
    xs, ys = el.lines[0].get_data()
    assert len(xs) <= 1000
    assert np.nanmax(ys) == pytest.approx(np.nanmax(y[:, 0]))  # the envelope keeps the extremes
    ax.set_xlim(50, 51)
    xs, _ = el.lines[0].get_data()
    assert len(xs) == pytest.approx(fs, abs=4)
    plt.close(fig)


def test_psd_shows_vibration_band(session):
    path, _ = session
    df, fs, _ = ex.load(path)
    f, still = ex.psd(df, fs, 0, 2.5, ex.ACCEL)
    f2, skating = ex.psd(df, fs, 20, 90, ex.ACCEL)
    band = f2 > 30
    assert skating[band].mean() > 20 * np.interp(f2[band], f, still).mean()
    assert ex.psd(df, fs, 500, 600, ex.ACCEL) is None


def test_cli_writes_files(session, tmp_path, capsys):
    path, _ = session
    main(["explore", str(path), "--png", str(tmp_path / "o.png"), "--psd", "0:3:still",
          "--psd", "20:90", "--psd-png", str(tmp_path / "p.png"), "--export", str(tmp_path / "x.csv"),
          "--start", "10", "--end", "200"])
    out = capsys.readouterr().out
    assert "cadence" in out and "1 gap(s)" in out
    assert (tmp_path / "o.png").stat().st_size > 10_000
    assert (tmp_path / "p.png").stat().st_size > 10_000
    x = pd.read_csv(tmp_path / "x.csv")
    assert x["t_s"].min() >= 10 and x["t_s"].max() < 200


def test_parse_range():
    assert ex.parse_range("1:2.5") == (1.0, 2.5, "1-2.5 s")
    assert ex.parse_range("10:20:rough asphalt") == (10.0, 20.0, "rough asphalt")
    with pytest.raises(ValueError):
        ex.parse_range("10")


def test_cli_writes_html(session, tmp_path, capsys):
    from bladetag.explore_html import decode

    path, _ = session
    out = tmp_path / "o.html"
    main(["explore", str(path), "--html", str(out), "--start", "10", "--end", "200",
          "--strides", str(tmp_path / "st.csv")])
    text = capsys.readouterr().out
    assert "cadence" in text and "strides         none found" in text   # skating, not walking
    assert list(pd.read_csv(tmp_path / "st.csv").columns)[:3] == ["t_s", "period_s", "stride_m"]
    html = out.read_text()
    assert "<title>" in html and "__BLOB__" not in html and "/*__META__*/" not in html
    meta, blob = decode(html)
    df = ex.crop(ex.load(path)[0], 10, 200)
    arr = {k: np.frombuffer(blob, a["dtype"], a["bytes"] // np.dtype(a["dtype"]).itemsize, a["offset"])
           for k, a in meta["arrays"].items()}
    assert meta["n"] == len(df) == len(arr["t"])
    np.testing.assert_allclose(arr["t"], df["t_s"], atol=1e-3)
    acc = arr["accel"].reshape(-1, 3) * meta["accel_scale"]
    np.testing.assert_allclose(acc, df[ex.ACCEL], atol=meta["accel_scale"])
    gyr = arr["gyro"].reshape(-1, 3) * meta["gyro_scale"]
    np.testing.assert_allclose(gyr, df[ex.GYRO], atol=meta["gyro_scale"])
    assert len(arr["seg_start"]) == len(meta["gaps"]) == 1   # the 40 s link drop
    assert meta["still"] and meta["cadence"]["t"] and meta["gps"]["t"] and meta["spec"]
    for s in meta["spec"]:
        assert len(arr[s["array"]]) == s["cols"] * s["rows"]
    assert meta["summary"] and "cadence" in "\n".join(meta["summary"])
