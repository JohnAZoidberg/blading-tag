"""``bladetag report``: several skating sessions on one self-contained page.

What's on it: the sessions side by side (also moved to one common speed),
findings written out in sentences, a scatter plot of any measure against any
other (one dot per stroke, a fit per session), the correlation matrix within
and across sessions, every measure along the way, a map of the route coloured
by a measure, places skated more than once compared directly, and coasting.

Everything is computed in ``skating``; this module turns it into sentences
and writes the page (``report.html`` in the package, data embedded as JSON,
no network access).
"""

from __future__ import annotations

import json
import math
from importlib import resources
from pathlib import Path

import numpy as np
import pandas as pd

from . import skating as sk

_TEMPLATE = "report.html"

# Pairs worth a sentence each, if the data has them: (x, y, what it means).
KEY_PAIRS = [
    ("push_rate_spm", "speed_kmh", "Faster with a quicker cadence?"),
    ("dist_per_push_m", "speed_kmh", "Faster with longer pushes?"),
    ("out_cm", "dist_per_push_m", "Wider pushes go further?"),
    ("out_cm", "speed_kmh", "Wider pushes, more speed?"),
    ("push_speed_mps", "speed_kmh", "A quicker push, more speed?"),
    ("pushoff_g", "dist_per_push_m", "A harder kick goes further?"),
    ("pushoff_g", "push_rate_spm", "Harder kicks at a quicker cadence?"),
    ("setdown_g", "speed_kmh", "Landing harder when faster?"),
    ("edge_deg", "dist_per_push_m", "More edge, more distance per push?"),
    ("push_rate_spm", "out_cm", "Quicker cadence, narrower push?"),
]

# A rough physical model for the equal-effort wind estimate.
RIDER = {"mass_kg": 80.0, "crr": 0.006, "cda_m2": 0.40, "rho": 1.2}


def _strength(r: float) -> str:
    a = abs(r)
    return ("no" if a < 0.1 else "a weak" if a < 0.3 else "a moderate" if a < 0.5
            else "a strong" if a < 0.7 else "a very strong")


def _fmt(key: str, v: float) -> str:
    m = sk.MEASURE.get(key)
    if v is None or not np.isfinite(v):
        return "-"
    s = (m.fmt if m else "{:.2f}").format(v)
    if not m or not m.unit or m.fmt.endswith("%}"):
        return s
    return s + m.unit if m.unit == "°" else f"{s} {m.unit}"


def wind_estimate(v_with: float, v_against: float, rider: dict = RIDER) -> float | None:
    """Wind speed (m/s) at which riding at ``v_with`` with it and at
    ``v_against`` against it takes the same power: rolling resistance plus
    air drag, P = v (Crr m g + 0.5 rho CdA (v -+ w)^2). Only meaningful if
    the effort really was the same both ways and nothing else differed."""
    a = rider["crr"] * rider["mass_kg"] * 9.81
    b = 0.5 * rider["rho"] * rider["cda_m2"]
    f = lambda w: v_with * (a + b * (v_with - w) * abs(v_with - w)) - v_against * (a + b * (v_against + w) ** 2)
    if f(0) <= 0:
        return None
    lo, hi = 0.0, 15.0
    if f(hi) > 0:
        return None
    for _ in range(60):
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if f(mid) > 0 else (lo, mid)
    return (lo + hi) / 2


def findings(df: pd.DataFrame, infos: list[sk.SessionInfo], corr: dict, pairs: pd.DataFrame,
             at: pd.DataFrame, speed: float) -> list[dict]:
    """Plain-language findings: {"title", "text", "x", "y"} (x/y: a scatter
    plot that shows it, if any)."""
    out = []
    keys = list(corr["within"].index)
    w, pooled = corr["within"], corr["pooled"]
    n_sess = df["session"].nunique()
    for x, y, q in KEY_PAIRS:
        if x not in keys or y not in keys:
            continue
        r = w.loc[x, y]
        if not np.isfinite(r):
            continue
        lx, ly = sk.MEASURE[x].label.lower(), sk.MEASURE[y].label.lower()
        txt = (f"Stroke to stroke {'within a session' if n_sess > 1 else ''}, {lx} and {ly} show "
               f"{_strength(r)} correlation (Spearman r = {r:+.2f}).")
        rp = pooled.loc[x, y]
        if n_sess > 1 and np.isfinite(rp) and abs(rp - r) > 0.25:
            txt += (f" Pooled over all sessions it's {rp:+.2f}: the sessions differ in both, so"
                    " something that changed between them (wind, foot, fatigue, road) shows up as well.")
        out.append({"kind": "pair", "title": q, "text": txt, "x": x, "y": y, "r": float(r), "verdict": _strength(r)})

    # Strongest within-session links with speed and distance per push not already covered.
    covered = {(f["x"], f["y"]) for f in out} | {(f["y"], f["x"]) for f in out}
    for target in ("speed_kmh", "dist_per_push_m"):
        if target not in keys:
            continue
        derived = {"speed_kmh", "dist_per_push_m", "period_s", "push_rate_spm"}
        cand = [(k, w.loc[k, target]) for k in keys if k not in derived and k != target
                and (k, target) not in covered and np.isfinite(w.loc[k, target])]
        cand.sort(key=lambda kv: -abs(kv[1]))
        for k, r in cand[:2]:
            if abs(r) < 0.2:
                continue
            out.append({"kind": "pair", "verdict": _strength(r),
                        "title": f"{sk.MEASURE[k].label} goes with {sk.MEASURE[target].label.lower()}",
                        "text": f"Among the measures not shown above, {sk.MEASURE[k].label.lower()} tracks "
                                f"{sk.MEASURE[target].label.lower()} most closely within a session (r = {r:+.2f}).",
                        "x": k, "y": target, "r": float(r)})

    # Out and back on the same road.
    if len(pairs):
        opp = pairs[pairs["direction"] == "opposite"].sort_values("places", ascending=False)
        for _, row in opp.head(2).iterrows():
            a, b = row["a"], row["b"]
            parts = []
            for k in ("speed_kmh", "push_rate_spm", "dist_per_push_m", "pushoff_g", "out_cm", "edge_deg"):
                va, vb = row.get(f"{k}_a"), row.get(f"{k}_b")
                if va is None or not np.isfinite(va) or not np.isfinite(vb) or not va:
                    continue
                parts.append(f"{sk.MEASURE[k].label.lower()} {_fmt(k, va)} → {_fmt(k, vb)} ({100 * (vb - va) / va:+.0f} %)")
            ia = next(i for i in infos if i.key == a)
            ib = next(i for i in infos if i.key == b)
            txt = (f"{a} and {b} cover the same {row['overlap_m'] / 1000:.1f} km in opposite directions "
                   f"({row['places']} strokes matched within 15 m). Same road and slope both ways, so the "
                   f"difference is wind, tiredness and the foot: " + "; ".join(parts) + ".")
            if ia.foot != ib.foot:
                txt += (f" The tag was on the {ia.foot.replace('_', ' ')} for {a} and the "
                        f"{ib.foot.replace('_', ' ')} for {b}, so a difference between your feet can't be told "
                        "apart from the direction here.")
            va, vb = row.get("speed_kmh_a"), row.get("speed_kmh_b")
            if np.isfinite(va) and np.isfinite(vb) and abs(va - vb) > 1:
                fast, slow = (va, vb) if va > vb else (vb, va)
                wv = wind_estimate(fast / 3.6, slow / 3.6)
                if wv:
                    txt += (f" If the effort had been the same both ways, the speed difference alone would mean "
                            f"a wind of about {wv * 3.6:.0f} km/h along the route (model: {RIDER['mass_kg']:.0f} kg, "
                            f"CdA {RIDER['cda_m2']} m², Crr {RIDER['crr']}).")
                    pa, pb = row.get("pushoff_g_a"), row.get("pushoff_g_b")
                    if np.isfinite(pa) and np.isfinite(pb):
                        slow_push = pb if vb < va else pa
                        fast_push = pa if vb < va else pb
                        if slow_push > fast_push * 1.03:
                            txt += " You kicked harder on the slow way, so the wind was probably stronger than that."
            out.append({"title": f"Out and back: {a} vs {b}", "text": txt, "x": "dist_per_push_m",
                        "y": "speed_kmh", "r": None})

    # Foot and direction confounded?
    feet = {i.foot for i in infos}
    if len(feet) > 1 and len(pairs) and (pairs["direction"] == "opposite").any():
        same_foot_pairs = [r for _, r in pairs.iterrows()
                           if next(i for i in infos if i.key == r["a"]).foot
                           == next(i for i in infos if i.key == r["b"]).foot and r["direction"] == "opposite"]
        if not same_foot_pairs:
            out.append({"title": "Next time: separate foot from direction",
                        "text": "Every out-and-back here has the tag on a different foot each way, so a left/right "
                                "difference looks exactly like a wind or tiredness difference. Next time keep the tag "
                                "on one foot for a whole out-and-back, or swap feet at the turn on one day and "
                                "the other way round on another. Then this report can tell them apart.",
                        "x": None, "y": None, "r": None})

    # Fatigue within the longest session.
    longest = max(infos, key=lambda i: i.summary.get("moving_s", 0))
    s = df[(df["session"] == longest.key) & df["moving"]]
    if len(s) > 100:
        n = max(20, len(s) // 5)
        first, last = s.head(n), s.tail(n)
        parts = []
        for k in ("speed_kmh", "push_rate_spm", "dist_per_push_m", "pushoff_g", "out_cm"):
            if k in s and s[k].notna().sum() > 40:
                a, b = first[k].median(), last[k].median()
                if a:
                    parts.append(f"{sk.MEASURE[k].label.lower()} {_fmt(k, a)} → {_fmt(k, b)} ({100 * (b - a) / a:+.0f} %)")
        out.append({"title": f"Start vs end of {longest.key}",
                    "text": f"First {n} strokes against the last {n} of the longest session: " + "; ".join(parts) + ".",
                    "x": "t_min", "y": "dist_per_push_m", "r": None})

    # Soft or hard.
    if "setdown_g" in df and df["setdown_g"].notna().any():
        m = df[df["moving"]]
        sd, po = m["setdown_g"].median(), m["pushoff_g"].median()
        r1 = w.loc["setdown_g", "dist_per_push_m"] if "dist_per_push_m" in keys else np.nan
        r2 = w.loc["pushoff_g", "dist_per_push_m"] if "dist_per_push_m" in keys else np.nan
        r3 = w.loc["pushoff_g", "push_rate_spm"] if "push_rate_spm" in keys else np.nan
        out.append({"title": "Soft or hard strides",
                    "text": f"The skate lands at {sd:.1f} g (median, road vibration filtered out) and the push ends in a "
                            f"kick of {po:.1f} g, the hardest moment of the stroke. Landing harder or softer doesn't change "
                            f"the distance per push (r = {r1:+.2f}), and neither does kicking harder (r = {r2:+.2f}); "
                            f"the hard kicks come with a quicker cadence (r = {r3:+.2f}).",
                    "x": "pushoff_g", "y": "dist_per_push_m", "r": float(r2) if np.isfinite(r2) else None})
    return out


def _clean(v):
    if isinstance(v, (float, np.floating)):
        return None if not math.isfinite(v) else round(float(v), 4)
    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, dict):
        return {k: _clean(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_clean(x) for x in v]
    return v


def build(df: pd.DataFrame, infos: list[sk.SessionInfo], title: str = "") -> dict:
    keys = sk.measures_present(df)
    corr = sk.correlations(df, keys)
    at, speed = sk.at_speed(df, keys)
    pairs = sk.route_pairs(df, keys=keys)
    cols = ["session", "t_s", "t_min", "moving", "lat", "lon", "bearing_deg"] + keys
    if "dist_km" in df:
        cols.append("dist_km")
    data = {c: _clean(df[c].tolist()) for c in cols if c in df}
    coasts = df.attrs.get("coasts", pd.DataFrame())
    return {
        "title": title or "Skating report",
        "measures": [{"key": m.key, "label": m.label, "unit": m.unit, "about": m.about,
                      "pct": m.fmt.endswith("%}")} for m in sk.MEASURES if m.key in keys]
                    + [{"key": "t_min", "label": "Time in session", "unit": "min", "about": "", "pct": False}],
        "sessions": [_clean({"key": i.key, "path": Path(i.path).name, "start_utc": i.start_utc, "gear": i.gear,
                             "notes": i.notes, "foot": i.foot, "warnings": i.warnings,
                             "distance_km": i.summary.get("distance_km"), "moving_min": i.summary.get("moving_s", 0) / 60,
                             "speed_kmh": i.summary.get("avg_moving_speed_kmh")}) for i in infos],
        "strokes": data,
        "corr": {k: _clean(v.to_numpy().tolist()) for k, v in corr.items()},
        "corr_keys": keys,
        "at_speed": {"speed_kmh": round(speed, 2), "rows": _clean(at.reset_index(names="session").to_dict("records"))},
        "pairs": _clean(pairs.to_dict("records")) if len(pairs) else [],
        "coasts": _clean(coasts.to_dict("records")) if len(coasts) else [],
        "findings": _clean(findings(df, infos, corr, pairs, at, speed)),
    }


def write_report(path: str | Path, report: dict) -> None:
    js = json.dumps(report, separators=(",", ":"), ensure_ascii=False).replace("</", "<\\/")
    page = resources.files("bladetag").joinpath(_TEMPLATE).read_text(encoding="utf-8")
    t = report["title"].replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    page = page.replace("__TITLE__", t).replace("/*__DATA__*/", js)
    Path(path).write_text(page, encoding="utf-8")


def session_paths(args: list[str]) -> list[Path]:
    """Session folders/zips from the arguments; a folder full of sessions
    stands for all of them."""
    out = []
    for a in args:
        p = Path(a)
        if p.is_dir() and not (p / "session.json").exists():
            out += sorted(q for q in p.iterdir() if (q / "session.json").exists() or q.suffix == ".zip")
        else:
            out.append(p)
    return out
