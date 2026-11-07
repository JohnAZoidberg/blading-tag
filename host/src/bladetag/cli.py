"""``bladetag`` command line."""

from __future__ import annotations

import argparse
import asyncio
import json
import signal
import sys
import threading
from pathlib import Path

from . import protocol as p


def _die(msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(1)


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
    print(f"session   {a.session}")
    print(f"recorder  {m['recorder']['name']} {m['recorder']['version']}, start {m['start_utc']}")
    if m.get("end_elapsed_ns"):
        print(f"length    {(m['end_elapsed_ns'] - m['start_elapsed_ns']) / 1e9:.1f} s")
    if m.get("notes"):
        print(f"notes     {m['notes']}")
    for t in s.tags:
        print(f"tag {t.meta.address} ({t.meta.position}), session_id {t.meta.session_id}, "
              f"{t.meta.odr_hz} Hz ±{t.meta.accel_range_g} g ±{t.meta.gyro_range_dps} dps")
        if t.info:
            print(f"  firmware {t.info.fw_string}, buffer {t.info.buffer_capacity}")
        print(f"  {len(t.pkt_n)} packets, {len(t.raw)} samples, {len(t.statuses)} status records"
              + (f", {t.unknown_packets} unknown packets" if t.unknown_packets else "")
              + (f", {t.bad_records} bad records" if t.bad_records else ""))
        if len(t.raw):
            fit = fit_clocks(t)
            print(f"  true rate {fit.rate_hz:.3f} Hz, clock fit ±{fit.rx_residual_ms:.2f} ms,"
                  f" skew {(fit.rx_clock.b / 1000 - 1) * 1e6:+.1f} ppm")
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


def cmd_analyze(a):
    from .analysis import analyze, format_summary
    from .session import load_session

    s = load_session(a.session)
    if not s.tags:
        _die("session has no tags")
    tags = s.tags
    if a.tag:
        tags = [t for t in s.tags if a.tag in (t.meta.position, t.meta.address)]
        if not tags:
            _die(f"no tag {a.tag!r}")
    for i, tag in enumerate(tags):
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
    c.add_argument("--fake-speed", type=float, default=1.0, help=argparse.SUPPRESS)
    c.set_defaults(fn=cmd_record)

    c = sub.add_parser("identify", help="blink the LED")
    c.add_argument("address", nargs="?", help=addr_help)
    c.add_argument("seconds", type=int, nargs="?", default=5)
    c.set_defaults(fn=cmd_identify)

    c = sub.add_parser("position", help="store which foot the tag is on")
    c.add_argument("address", nargs="?", help=addr_help)
    c.add_argument("position", choices=["left", "right", "unknown"])
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
    c.set_defaults(fn=cmd_analyze)

    a = ap.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
