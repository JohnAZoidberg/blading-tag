"""``bladetag`` command line."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import signal
import sys
import threading
from pathlib import Path

from . import protocol as p
from .protocol import ACTIVITIES


def _die(msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(1)


def _warn_simulated(s) -> None:
    """On stderr, so it doesn't end up in --json output."""
    if s.simulated:
        print("note: SIMULATED session, fake data from a simulator, not a real recording", file=sys.stderr)


# --- BLE commands ------------------------------------------------------------


async def _pick_address(addr: str | None) -> str:
    if addr:
        return addr
    from .link import scan

    print("scanning ...")
    found = await scan(5.0)
    if len(found) == 1:
        print(f"using {found[0].address} ({found[0].name})")
        return found[0].address
    if not found:
        _die("no tag found. Shake it to wake it up, or pass an address.")
    for f in found:
        print(f"  {f.address}  {f.name}")
    _die("more than one tag found; pass an address")


def cmd_scan(a):
    from .link import scan

    async def go():
        found = await scan(a.timeout)
        if not found:
            print("no tags found")
        for f in found:
            ad = f.advert
            extra = (f"v{ad.protocol_version} {ad.state.name:<9} {ad.position.label:<7} {ad.battery_mv} mV"
                     if ad else "(no manufacturer data)")
            print(f"{f.address}  {f.name or '?':<14} {f.rssi or 0:4d} dBm  {extra}")

    asyncio.run(go())


def cmd_info(a):
    from .link import open_link

    async def go():
        addr = await _pick_address(a.address)
        async with open_link(addr) as link:
            info = p.Info.parse(await link.read_info())
            st = p.Status.parse(await link.read_status())
            print(f"address     {addr}  (MTU {link.mtu})")
            print(f"firmware    {info.fw_string}, protocol v{info.protocol_version}")
            print(f"rates       {info.odr_hz} Hz")
            print(f"accel       ±{info.accel_ranges_g} g, gyro ±{info.gyro_ranges_dps} dps")
            print(f"buffer      {info.buffer_capacity} samples")
            print(f"state       {st.state.name}, position {st.position.label}, {st.battery_mv} mV,"
                  f" flags {st.flag_names() or '-'}")
            print(f"session     {st.session_id}: {st.odr_hz} Hz ±{st.accel_range_g} g ±{st.gyro_range_dps} dps,"
                  f" {st.samples_recorded} recorded, {st.samples_dropped} dropped")
            print(f"tag time    {st.tag_time_us / 1e6:.3f} s since boot")

    asyncio.run(go())


def _control(a, data: bytes, what: str):
    from .link import ControlError, open_link

    async def go():
        addr = await _pick_address(a.address)
        async with open_link(addr) as link:
            try:
                await link.write_control(data)
            except ControlError as e:
                _die(str(e))
            print(what)

    asyncio.run(go())


def cmd_identify(a):
    _control(a, p.cmd_identify(a.seconds), f"blinking for {a.seconds} s")


def cmd_position(a):
    _control(a, p.cmd_set_position(p.Position.from_label(a.position)), f"position set to {a.position}")


def cmd_stop(a):
    _control(a, p.cmd_stop(), "stopped")


def cmd_sleep(a):
    _control(a, p.cmd_sleep(), "tag is going to sleep")


def cmd_record(a):
    from .link import ControlError, open_link
    from .recorder import RecordError, RecordOptions, Recorder
    from .session import SessionWriter

    if a.address and a.address.startswith("fake"):
        from .faketag import FakeTag

        FakeTag.shared(a.address).speed = a.fake_speed

    addr = asyncio.run(_pick_address(a.address)) if not a.address else a.address
    folder = SessionWriter.new_folder(Path(a.out))
    writer = SessionWriter(folder)
    writer.meta["notes"] = a.notes or ""
    writer.meta["activity"] = a.activity
    writer.meta["gear"] = (a.gear or "").strip()
    opts = RecordOptions(a.odr, a.accel_range, a.gyro_range, a.duration)

    ring = None
    if a.plot:
        from .liveplot import Ring

        ring = Ring(rate=a.odr or p.DEFAULT_ODR_HZ)

    state = {}

    async def go():
        loop = asyncio.get_running_loop()
        stop = asyncio.Event()
        state["loop"], state["stop"] = loop, stop
        rec = Recorder(open_link(addr), writer, opts, on_packet=ring.push if ring else None)

        async def ticker():
            while True:
                await asyncio.sleep(1.0)
                print("\r" + rec.stats.line() + "   ", end="", flush=True)

        tick = loop.create_task(ticker())
        try:
            await rec.run(stop)
        finally:
            tick.cancel()
            print()

    def request_stop():
        if "loop" in state:
            state["loop"].call_soon_threadsafe(state["stop"].set)

    def run_recorder():
        try:
            asyncio.run(go())
        except (ControlError, RecordError, TimeoutError) as e:
            state["error"] = e
        finally:
            done.set()

    print(f"recording to {folder}  (Ctrl-C to stop)")
    done = threading.Event()
    th = threading.Thread(target=run_recorder, daemon=True)
    old = signal.signal(signal.SIGINT, lambda *_: request_stop())
    th.start()
    try:
        if ring is not None:
            from .liveplot import run_live_plot

            run_live_plot(ring, f"bladetag {addr}", done)
            request_stop()  # window closed
        while not done.wait(0.2):
            pass
    finally:
        signal.signal(signal.SIGINT, old)
    th.join()
    if "error" in state:
        _die(str(state["error"]))
    print(f"saved {folder}")


# --- offline commands --------------------------------------------------------


def cmd_simulate(a):
    from . import sim

    model = sim.SkaterModel(duration_s=a.duration, side=a.side, seed=a.seed)
    link = sim.LinkModel(drops=[tuple(map(float, d.split(":"))) for d in a.drop])
    sim.write_session(Path(a.out), model, link, with_gps=not a.no_gps)
    print(f"wrote {a.out}")


def cmd_dump(a):
    from .session import load_session
    from .timesync import fit_clocks

    s = load_session(a.session)
    m = s.meta
    print(f"session   {a.session}" + ("  (SIMULATED)" if s.simulated else ""))
    print(f"recorder  {m['recorder']['name']} {m['recorder']['version']}, start {m['start_utc']}")
    if m.get("end_elapsed_ns"):
        print(f"length    {(m['end_elapsed_ns'] - m['start_elapsed_ns']) / 1e9:.1f} s")
    if m.get("end_reason"):
        print(f"ended     {m['end_reason']}" + (f" ({m['end_detail']})" if m.get("end_detail") else ""))
    elif not m.get("end_elapsed_ns"):
        print("ended     unfinished (recorder died before saving the end)")
    print(f"activity  {s.activity}" + (f", gear: {s.gear}" if s.gear else ""))
    if m.get("notes"):
        print(f"notes     {m['notes']}")
    for t in s.tags:
        print(f"tag {t.meta.address} ({t.meta.position}{', simulated' if t.meta.simulated else ''}),"
              f" session_id {t.meta.session_id}, "
              f"{t.meta.odr_hz} Hz ±{t.meta.accel_range_g} g ±{t.meta.gyro_range_dps} dps")
        if t.info:
            print(f"  firmware {t.info.fw_string}, buffer {t.info.buffer_capacity}")
        print(f"  {len(t.pkt_n)} packets, {len(t.raw)} samples, {len(t.statuses)} status records"
              + (f", {t.unknown_packets} unknown packets" if t.unknown_packets else "")
              + (f", {t.bad_records} bad records" if t.bad_records else "")
              + (f", {t.duplicate_packets} repeated packets dropped (resends after a reconnect)" if t.duplicate_packets else ""))
        if len(t.raw):
            fit = fit_clocks(t)
            if math.isfinite(fit.rx_residual_ms):
                print(f"  true rate {fit.rate_hz:.3f} Hz, clock fit ±{fit.rx_residual_ms:.2f} ms,"
                      f" skew {(fit.rx_clock.b / 1000 - 1) * 1e6:+.1f} ppm")
            else:
                print(f"  true rate {fit.rate_hz:.3f} Hz, clock fit n/a (recording too short)")
        for start, n in t.gaps():
            print(f"  gap: {n} samples missing from index {start}")
        if t.seq_gaps():
            print(f"  {t.seq_gaps()} packets lost (seq)")
        if t.statuses:
            mv = [st.battery_mv for _, st in t.statuses]
            last = t.statuses[-1][1]
            print(f"  battery {min(mv)}-{max(mv)} mV; last status {last.state.name}, "
                  f"recorded {last.samples_recorded}, dropped {last.samples_dropped}, flags {last.flag_names() or '-'}")
    if s.gps is not None:
        print(f"gps       {len(s.gps)} fixes")


def _tags(s, which: str | None):
    if not s.tags:
        _die("session has no tags")
    if not which:
        return s.tags
    tags = [t for t in s.tags if p.normalize_position(which) in (t.meta.position, t.meta.address)]
    if not tags:
        _die(f"no tag {which!r}")
    return tags


def _walk(s, tag):
    """(strides, gps frame) for one tag, as in `explore`."""
    from . import explore as ex
    from . import walking
    from .timesync import fit_clocks

    clock = fit_clocks(tag)
    df = ex.sample_frame(s, tag, clock)
    return walking.strides(df, clock.rate_hz), ex.gps_frame(s)


def cmd_analyze(a):
    from .session import load_session

    s = load_session(a.session)
    _warn_simulated(s)
    tags = _tags(s, a.tag)
    activity = a.activity or s.activity
    if activity == "walking":
        return _analyze_walking(a, s, tags)
    if activity == "cycling":
        _die("there's no cycling analysis yet (it waits for a first ride, see PLAN-activities.md);"
             f" `bladetag explore {a.session}` shows the raw signal and cadence")
    if activity == "unknown":
        _die(f"the session doesn't say what activity it is. Use --activity skating|walking, set it with"
             f" `bladetag label {a.session} --activity ...`, or look at it with `bladetag explore`")
    _analyze_skating(a, s, tags)


def _analyze_walking(a, s, tags):
    from . import walking

    if a.plot or a.show or a.raw:
        _die("--plot/--show/--raw are for skating; for walking use `bladetag explore --html`")
    for tag in tags:
        st, gps = _walk(s, tag)
        suffix = f"-{tag.meta.position}" if len(tags) > 1 else ""
        if a.json:
            g = st.dropna(subset=["strike_deg"])
            print(json.dumps({"activity": "walking", "gear": s.gear, "tag": tag.meta.address,
                              "strides": len(st), **{c: g[c].median() for c in g.columns if c != "t_s"}},
                             indent=2, default=float))
        else:
            print(f"tag {tag.meta.address} ({tag.meta.position}), walking" + (f" in {s.gear}" if s.gear else ""))
            print("\n".join(walking.describe(st, gps)))
        if a.csv:
            st.to_csv(_suffixed(a.csv, suffix), index=False, float_format="%.4f")


def _analyze_skating(a, s, tags):
    from .analysis import analyze, format_summary

    for tag in tags:
        r = analyze(s, tag, method=a.method)
        if a.json:
            print(json.dumps(r.summary, indent=2, default=float))
        else:
            print(format_summary(r))
            if r.per_km is not None and not a.no_table:
                print()
                print(r.per_km.round(2).to_string())
        suffix = f"-{tag.meta.position}" if len(tags) > 1 else ""
        if a.csv:
            r.strokes.to_csv(_suffixed(a.csv, suffix), index=False)
        if a.plot or a.show or a.raw:
            import matplotlib

            if not a.show:
                matplotlib.use("Agg")
            from .plots import plot_raw, plot_session

            if a.plot or a.show:
                plot_session(r, _suffixed(a.plot, suffix) if a.plot else None, show=a.show)
            if a.raw:
                plot_raw(r, a.raw_at, path=_suffixed(a.raw, suffix))


def cmd_report(a):
    from . import report, skating

    paths = report.session_paths(a.sessions)
    if not paths:
        _die("no sessions found")
    df, infos = skating.load_sessions(paths, progress=lambda m: print(m, file=sys.stderr))
    if df.empty:
        _die("no skating session with stroke cycles among these")
    if a.csv:
        df.drop(columns=["moving"]).to_csv(a.csv, index=False, float_format="%.4g")
        print(f"wrote {a.csv} ({len(df)} strokes)")
    rep = report.build(df, infos, a.title or "")
    for f in rep["findings"]:
        print(f"\n{f['title']}\n  {f['text']}")
    report.write_report(a.output, rep)
    print(f"\nwrote {a.output} (open it in a browser; works offline)")


def cmd_explore(a):
    from . import explore as ex
    from . import walking
    from .session import load_session
    from .timesync import fit_clocks

    s = load_session(a.session)
    _warn_simulated(s)
    if a.strides and s.activity not in ("walking", "unknown"):
        _die(f"--strides is for walking, and this session is {s.activity}")
    tags = s.tags
    if a.tag:
        tags = [t for t in s.tags if p.normalize_position(a.tag) in (t.meta.position, t.meta.address)]
    if not tags:
        _die(f"no tag {a.tag!r}" if a.tag else "session has no tags")
    try:
        ranges = [ex.parse_range(r) for r in a.psd]
    except ValueError as e:
        _die(str(e))
    if a.export and Path(a.export).suffix not in (".csv", ".parquet"):
        _die("--export needs a .csv or .parquet file name")
    show = not (a.png or a.psd_png or a.export or a.html or a.strides)
    if show or a.png or a.psd_png:
        import matplotlib

        if not show:
            matplotlib.use("Agg")
    gps = ex.gps_frame(s)
    if gps is not None and (a.start is not None or a.end is not None):
        gps = gps[(gps["t_s"] >= (a.start if a.start is not None else -float("inf")))
                  & (gps["t_s"] < (a.end if a.end is not None else float("inf")))]
    for tag in tags:
        if len(tag.raw) == 0:
            print(f"tag {tag.meta.address}: no IMU samples")
            continue
        clock = fit_clocks(tag)
        fs = clock.rate_hz
        try:
            df = ex.crop(ex.sample_frame(s, tag, clock), a.start, a.end)
        except ValueError as e:
            _die(str(e))
        name = f"{tag.meta.address} ({tag.meta.position})" + (" SIMULATED" if s.simulated else "")
        still = ex.still_mask(df, fs)
        cad = ex.cadence_track(df, fs)
        print(f"tag {name}")
        summary = ex.describe(df, fs, tag.meta.accel_range_g, tag.meta.gyro_range_dps, gps, cad, still)
        st = None
        if s.activity in ("walking", "unknown"):
            st = walking.strides(df, fs, still)
            summary += walking.describe(st, gps)
        print("\n".join(summary))
        suffix = f"-{tag.meta.position}" if len(tags) > 1 else ""
        if a.export:
            ex.export(df, _suffixed(a.export, suffix))
        if a.strides and st is not None:
            st.to_csv(_suffixed(a.strides, suffix), index=False, float_format="%.4f")
        if a.html:
            from .explore_html import write_overview_html

            write_overview_html(_suffixed(a.html, suffix), df, fs, gps, title=name, cad=cad, still=still,
                                summary=summary, strides=st, accel_range_g=tag.meta.accel_range_g,
                                gyro_range_dps=tag.meta.gyro_range_dps)
        if show or a.png:
            ex.plot_overview(df, fs, gps, title=name, cad=cad, still=still,
                             path=_suffixed(a.png, suffix) if a.png else None)
        if ranges and (show or a.psd_png):
            ex.plot_psd(df, fs, ranges, title=name, path=_suffixed(a.psd_png, suffix) if a.psd_png else None)
    if show:
        import matplotlib.pyplot as plt

        plt.show()


def cmd_viz3d(a):
    from . import viz3d
    from .session import load_session

    motions = []
    for path in a.sessions:
        s = load_session(path)
        _warn_simulated(s)
        if s.activity not in ("skating", "unknown"):
            print(f"{path}: skipped, it's a {s.activity} session (the 3D view is for skating)", file=sys.stderr)
            continue
        for tag in _tags(s, a.tag):
            try:
                fm = viz3d.foot_motion(s, tag)
            except ValueError as e:
                print(f"{path}: skipped, {e}", file=sys.stderr)
                continue
            print("\n".join(viz3d.describe(fm)))
            motions.append(fm)
    if not motions:
        _die("nothing to show: no skating session with stroke cycles")
    out = viz3d.write_html(motions, a.output)
    print(f"wrote {out} (open it in a browser; three.js and the fonts load from a CDN)")


def cmd_label(a):
    from .session import set_labels

    if a.activity is None and a.gear is None and a.notes is None:
        _die("nothing to change: give --activity, --gear or --notes")
    for path in a.sessions:
        try:
            m = set_labels(path, a.activity, a.gear, a.notes)
        except (FileNotFoundError, ValueError) as e:
            _die(str(e))
        print(f"{path}: {m.get('activity', 'unknown')}" + (f", {m['gear']}" if m.get("gear") else ""))


def cmd_compare(a):
    from . import walking
    from .session import load_session

    runs = []
    for path in a.sessions:
        s = load_session(path)
        if s.simulated:
            print(f"note: {path} is SIMULATED", file=sys.stderr)
        if s.activity not in ("walking", "unknown"):
            print(f"skipping {path}: it's {s.activity}, and compare is for walking", file=sys.stderr)
            continue
        name = Path(path).name.removesuffix(".zip")
        group = {"gear": s.gear or name, "activity": s.activity, "session": name}[a.by]
        feet = [t for t in _tags(s, a.tag) if t.meta.position.endswith("_foot") or t.meta.position == "unknown"]
        for tag in feet:
            st, _ = _walk(s, tag)
            label = f"{name} ({tag.meta.position})" if len(feet) > 1 else name
            runs.append((group, label, st))
    if not runs:
        _die("no walking sessions to compare")
    try:
        per, groups, speed = walking.compare(runs, a.speed, a.band)
    except ValueError as e:
        _die(str(e))
    print(walking.format_compare(per, groups, speed, a.band))
    if a.csv:
        per.to_csv(a.csv, index=False, float_format="%.4f")


def _suffixed(path: str, suffix: str) -> str:
    pth = Path(path)
    return str(pth.with_name(pth.stem + suffix + pth.suffix))


# --- argparse ----------------------------------------------------------------


def main(argv=None):
    ap = argparse.ArgumentParser(prog="bladetag", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    addr_help = "tag address (default: the only tag in range); 'fake' for a simulated tag"

    c = sub.add_parser("scan", help="list tags in range")
    c.add_argument("-t", "--timeout", type=float, default=5.0)
    c.set_defaults(fn=cmd_scan)

    c = sub.add_parser("info", help="read Info and Status")
    c.add_argument("address", nargs="?", help=addr_help)
    c.set_defaults(fn=cmd_info)

    c = sub.add_parser("record", help="record a session")
    c.add_argument("address", nargs="?", help=addr_help)
    c.add_argument("-o", "--out", default="sessions", help="parent folder (default: ./sessions)")
    c.add_argument("--odr", type=int, default=0, choices=[0, *p.ODR_VALUES], help="Hz (0 = tag default)")
    c.add_argument("--accel-range", type=int, default=0, choices=[0, *p.ACCEL_RANGES], help="g")
    c.add_argument("--gyro-range", type=int, default=0, choices=[0, *p.GYRO_RANGES], help="dps")
    c.add_argument("-d", "--duration", type=float, help="stop after this many seconds")
    c.add_argument("-p", "--plot", action="store_true", help="live plot")
    c.add_argument("-n", "--notes", help="free text for session.json")
    c.add_argument("-a", "--activity", choices=ACTIVITIES, default="unknown", help="what you're doing")
    c.add_argument("-g", "--gear", help="the shoes, skates or bike, e.g. 'Brooks Ghost 15'")
    c.add_argument("--fake-speed", type=float, default=1.0, help=argparse.SUPPRESS)
    c.set_defaults(fn=cmd_record)

    c = sub.add_parser("identify", help="blink the LED")
    c.add_argument("address", nargs="?", help=addr_help)
    c.add_argument("seconds", type=int, nargs="?", default=5)
    c.set_defaults(fn=cmd_identify)

    c = sub.add_parser("position", help="store where on the body the tag is worn")
    c.add_argument("address", nargs="?", help=addr_help)
    c.add_argument("position", choices=[x.label for x in p.Position])
    c.set_defaults(fn=cmd_position)

    c = sub.add_parser("stop", help="stop a recording that was left running")
    c.add_argument("address", nargs="?", help=addr_help)
    c.set_defaults(fn=cmd_stop)

    c = sub.add_parser("sleep", help="put the tag into System OFF")
    c.add_argument("address", nargs="?", help=addr_help)
    c.set_defaults(fn=cmd_sleep)

    c = sub.add_parser("simulate", help="write a synthetic session (for testing)")
    c.add_argument("out")
    c.add_argument("--duration", type=float, default=600.0, help="seconds")
    c.add_argument("--side", choices=["left", "right"], default="left")
    c.add_argument("--seed", type=int, default=0)
    c.add_argument("--drop", action="append", default=[], metavar="START:DUR",
                   help="link drop at START s for DUR s (repeatable)")
    c.add_argument("--no-gps", action="store_true")
    c.set_defaults(fn=cmd_simulate)

    c = sub.add_parser("dump", help="summarise a session's raw contents")
    c.add_argument("session", help="session folder or .zip")
    c.set_defaults(fn=cmd_dump)

    c = sub.add_parser("analyze", help="push rate, contact/air, distance per push, fatigue")
    c.add_argument("session", help="session folder or .zip")
    c.add_argument("--tag", help="left, right or an address (default: all)")
    c.add_argument("--plot", metavar="PNG", help="save the overview plot")
    c.add_argument("--raw", metavar="PNG", help="save a raw-signal debug plot")
    c.add_argument("--raw-at", type=float, help="start time (s) for --raw")
    c.add_argument("--show", action="store_true", help="show the overview plot")
    c.add_argument("--csv", metavar="FILE", help="write per-stroke table")
    c.add_argument("--json", action="store_true", help="print the summary as JSON")
    c.add_argument("--no-table", action="store_true", help="skip the per-km table")
    c.add_argument("--method", choices=["auto", "setdown", "gyro"], default="auto",
                   help="cycle boundaries: wheel set-down (vibration) or |gyro| peaks")
    c.add_argument("-a", "--activity", choices=ACTIVITIES[:-1],
                   help="analyze as this activity (default: the session's own)")
    c.set_defaults(fn=cmd_analyze)

    c = sub.add_parser("label", help="set a session's activity, gear (shoes, skates, bike) or notes",
                       description="Change session.json in session folders, e.g. to mark old sessions"
                                   " (they're all 'unknown') or to fix a typo in the gear.")
    c.add_argument("sessions", nargs="+", help="session folders")
    c.add_argument("-a", "--activity", choices=ACTIVITIES)
    c.add_argument("-g", "--gear", help="e.g. 'Brooks Ghost 15'; '' to clear")
    c.add_argument("-n", "--notes")
    c.set_defaults(fn=cmd_label)

    c = sub.add_parser("compare", help="walks side by side at one speed, e.g. to compare shoes",
                       description="Gait measures (stride, stance, foot angle at heel strike and toe-off,"
                                   " impact, lift) for each walk at a common speed, grouped by gear, with"
                                   " the spread between walks in the same gear.")
    c.add_argument("sessions", nargs="+", help="session folders or .zip files")
    c.add_argument("--by", choices=["gear", "session", "activity"], default="gear",
                   help="what to group by (default: gear; sessions without one count as their own)")
    c.add_argument("--speed", type=float, help="km/h to compare at (default: median over all strides)")
    c.add_argument("--band", type=float, default=1.0, help="use strides within this many km/h (default: 1)")
    c.add_argument("--tag", help="left, right or an address (default: every foot tag)")
    c.add_argument("--csv", metavar="FILE", help="write the per-walk table")
    c.set_defaults(fn=cmd_compare)

    c = sub.add_parser("explore", help="look at the raw signal: zoomable overview, spectra, export",
                       description="Print a summary and open a zoomable overview of the raw signal. "
                                   "With --html/--png/--psd-png/--export/--strides, write files instead of "
                                   "opening windows. Stride lengths are for walking only.")
    c.add_argument("session", help="session folder or .zip")
    c.add_argument("--tag", help="left, right or an address (default: all)")
    c.add_argument("--start", type=float, help="only from this time (s since session start)")
    c.add_argument("--end", type=float, help="only up to this time (s)")
    c.add_argument("--psd", action="append", default=[], metavar="START:END[:LABEL]",
                   help="compare spectra of time ranges (repeatable)")
    c.add_argument("--html", metavar="HTML",
                   help="save the overview as a self-contained, zoomable page (no matplotlib needed)")
    c.add_argument("--png", metavar="PNG", help="save the overview")
    c.add_argument("--psd-png", metavar="PNG", help="save the --psd comparison")
    c.add_argument("--export", metavar="FILE", help="write every sample as .csv or .parquet")
    c.add_argument("--strides", metavar="CSV", help="write each walking stride (length, period, speed) as .csv")
    c.set_defaults(fn=cmd_explore)

    c = sub.add_parser("viz3d", help="3D replay of how the skate moved, stroke by stroke",
                       description="Work out the skate's path and angles relative to the skater for every stroke "
                                   "and write a page that replays them in 3D, with the typical stroke early vs "
                                   "late in the session. Several sessions go into one page.")
    c.add_argument("sessions", nargs="+", help="session folders or .zip files")
    c.add_argument("--tag", help="left, right or an address (default: all)")
    c.add_argument("-o", "--output", default="strokes3d.html", help="page to write (default: strokes3d.html)")
    c.set_defaults(fn=cmd_viz3d)

    c = sub.add_parser("report", help="skating sessions on one page: findings, correlations, map, out vs back",
                       description="Measure every stroke of every skating session given (push rate, distance per "
                                   "push, set-down impact, push-off, foot path, GPS speed and heading) and write one "
                                   "self-contained page that compares them: findings in words, any measure against "
                                   "any other, the correlation matrix, a map, places skated more than once, and "
                                   "the sessions at one common speed. A folder of sessions stands for all of them.")
    c.add_argument("sessions", nargs="+", help="session folders, .zip files, or a folder of sessions")
    c.add_argument("-o", "--output", default="report.html", help="page to write (default: report.html)")
    c.add_argument("--csv", metavar="FILE", help="also write every stroke as .csv")
    c.add_argument("--title", help="page title")
    c.set_defaults(fn=cmd_report)

    a = ap.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
