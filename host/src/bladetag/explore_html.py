"""``bladetag explore --html``: the overview as one self-contained HTML file.

Same panels as ``explore.plot_overview`` (accel, gyro, spectrogram, cadence,
GPS speed, rest and gap shading), drawn in the browser so it can be zoomed
and panned without Python or matplotlib. No network access is needed: the
page's script is inline and the samples are embedded.

The samples go in as int16 at the sensor's own resolution (the configured
range over 32767) with float32 timestamps, zlib-compressed and base64'd; the
page inflates them with the browser's DecompressionStream. A 20 min session
is about 4 MB.
"""

from __future__ import annotations

import base64
import json
import zlib
from importlib import resources
from pathlib import Path

import numpy as np
import pandas as pd

from .explore import ACCEL, ACTIVE_GYRO_DPS, GYRO, VIB_HP_HZ, _runs, cadence_track, spectrogram, still_mask

_TEMPLATE = "explore_overview.html"


def _int16(x: np.ndarray, full_scale: float) -> tuple[np.ndarray, float]:
    """Quantise to int16 with ``full_scale`` (or the data's peak, if larger) at 32767."""
    peak = float(np.nanmax(np.abs(x))) if x.size else 0.0
    scale = max(full_scale, peak, 1e-9) / 32767
    return np.round(np.nan_to_num(x) / scale).astype("<i2"), scale


def payload(df: pd.DataFrame, fs: float, gps: pd.DataFrame | None = None, title: str = "",
            cad: pd.DataFrame | None = None, still: np.ndarray | None = None, summary: list[str] | None = None,
            accel_range_g: float = 16, gyro_range_dps: float = 2000) -> tuple[dict, bytes]:
    """(meta, blob): what the page embeds. ``meta`` is JSON-able and names
    the byte ranges of the arrays in the (uncompressed) ``blob``."""
    t = df["t_s"].to_numpy()
    seg = df["segment"].to_numpy()
    still = still_mask(df, fs) if still is None else still
    cad = cadence_track(df, fs) if cad is None else cad

    arrays: dict[str, np.ndarray] = {}
    meta: dict = {"title": title, "summary": summary or [], "fs": fs, "n": len(df),
                  "vib_hp_hz": VIB_HP_HZ, "accel_cols": ACCEL, "gyro_cols": GYRO}
    arrays["t"] = t.astype("<f4")
    arrays["seg_start"] = (np.flatnonzero(np.diff(seg) != 0) + 1).astype("<i4")
    arrays["accel"], meta["accel_scale"] = _int16(df[ACCEL].to_numpy(), accel_range_g)
    arrays["gyro"], meta["gyro_scale"] = _int16(df[GYRO].to_numpy(), gyro_range_dps)

    rs, re = _runs(still)
    meta["still"] = [[float(t[i]), float(t[j - 1])] for i, j in zip(rs, re)]
    brk = arrays["seg_start"]
    meta["gaps"] = [[float(t[i - 1]), float(t[i])] for i in brk]

    specs = spectrogram(df, fs)
    meta["spec"] = []
    if specs:
        db = [10 * np.log10(s + 1e-12) for _, _, s in specs]
        vmin, vmax = np.percentile(np.concatenate([d.ravel() for d in db]), [5, 99.5])
        meta["spec_db"] = [float(vmin), float(vmax)]
        for k, ((tt, f, _), d) in enumerate(zip(specs, db)):
            # Columns are evenly spaced within a segment; rows are 0..fs/2.
            q = np.clip((d - vmin) / (vmax - vmin or 1) * 255, 0, 255).astype(np.uint8)
            name = f"spec{k}"
            arrays[name] = np.ascontiguousarray(q.T)  # (time, freq): one column per row
            dt = float(np.median(np.diff(tt))) if len(tt) > 1 else 128 / fs
            meta["spec"].append({"array": name, "t0": float(tt[0]), "dt": dt, "cols": len(tt),
                                 "rows": len(f), "f_max": float(f[-1])})

    c = cad[cad["gyro_rms_dps"] > ACTIVE_GYRO_DPS] if len(cad) else cad
    meta["cadence"] = {"t": np.round(c["t_s"], 3).tolist() if len(c) else [],
                       "cpm": np.round(60 / c["period_s"], 2).tolist() if len(c) else [],
                       "strength": np.round(c["strength"], 3).tolist() if len(c) else []}
    if gps is not None and len(gps):
        g = gps[np.isfinite(gps["speed_mps"])]
        meta["gps"] = {"t": np.round(g["t_s"], 3).tolist(), "kmh": np.round(g["speed_mps"] * 3.6, 2).tolist()}

    parts, offsets, pos = [], {}, 0
    for name, arr in arrays.items():
        b = arr.tobytes()
        offsets[name] = {"offset": pos, "bytes": len(b), "dtype": arr.dtype.str}
        parts.append(b)
        pos += len(b)
    meta["arrays"] = offsets
    return meta, b"".join(parts)


def decode(html: str) -> tuple[dict, bytes]:
    """The inverse of what ``write_overview_html`` embeds; for tests."""
    def between(a: str, b: str) -> str:
        i = html.index(a) + len(a)
        return html[i:html.index(b, i)]

    meta = json.loads(between('<script type="application/json" id="meta">', "</script>"))
    blob = zlib.decompress(base64.b64decode(between('<script type="text/plain" id="blob">', "</script>")))
    return meta, blob


def write_overview_html(path: str | Path, df: pd.DataFrame, fs: float, gps: pd.DataFrame | None = None,
                        title: str = "", cad: pd.DataFrame | None = None, still: np.ndarray | None = None,
                        summary: list[str] | None = None, accel_range_g: float = 16,
                        gyro_range_dps: float = 2000) -> None:
    meta, blob = payload(df, fs, gps, title, cad, still, summary, accel_range_g, gyro_range_dps)
    # "</" can't appear inside a <script>; JSON allows escaping the slash.
    meta_json = json.dumps(meta, separators=(",", ":")).replace("</", "<\\/")
    b64 = base64.b64encode(zlib.compress(blob, 6)).decode("ascii")
    page = resources.files("bladetag").joinpath(_TEMPLATE).read_text(encoding="utf-8")
    page = (page.replace("__TITLE__", _html_escape(title or "blading-tag session"))
                .replace("/*__META__*/", meta_json)
                .replace("__BLOB__", b64))
    Path(path).write_text(page, encoding="utf-8")


def _html_escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
