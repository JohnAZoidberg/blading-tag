#!/usr/bin/env python3
"""Write a fixture for the Android app's live push-rate test.

The app estimates push rate on the phone with a Kotlin port of
``explore.cadence_track``. This exports a session's gyro, decimated the way the
app decimates it (means of 4 samples at 200 Hz), plus the host's cadence_track
result, so the Kotlin test can check it gets the same periods:

    .venv/bin/python live_parity.py ../sessions/session-... /tmp/parity.csv
    LIVE_PARITY_CSV=/tmp/parity.csv make -C ../android test

Uses the longest gap-free stretch of the first tag.
"""
import sys

from bladetag.explore import GYRO, cadence_track, load


def main(session: str, out: str) -> None:
    df, fs, _ = load(session)
    seg = df["segment"].value_counts().idxmax()
    d = df[df["segment"] == seg].reset_index(drop=True)
    d["t_s"] -= d["t_s"].iloc[0]
    f = max(1, round(fs / 50))
    g = d[GYRO].to_numpy()
    m = len(g) // f * f
    g50 = g[:m].reshape(-1, f, 3).mean(axis=1)
    cad = cadence_track(d, fs)
    moving = cad[cad["gyro_rms_dps"] > 30]
    with open(out, "w") as o:
        o.write(f"#fs {fs / f:.4f}\n")
        for r in g50:
            o.write("%.3f,%.3f,%.3f\n" % tuple(r))
        for r in moving.itertuples():
            o.write(f"# {r.t_s:.3f},{r.period_s:.4f},{r.strength:.3f}\n")
    rep = int((moving["strength"] > 0.5).sum())
    print(f"{len(g50)} samples at {fs / f:.2f} Hz, {len(moving)} moving windows, {rep} repetitive")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    main(sys.argv[1], sys.argv[2])
