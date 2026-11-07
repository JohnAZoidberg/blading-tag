# host

Python tools that run on a laptop: a BLE recorder with a live plot, and the
session analysis. Phases 4 and 6 in [../PLAN.md](../PLAN.md). They implement
both interfaces in [../PROTOCOL.md](../PROTOCOL.md).

> **Status:** everything has been checked against simulated data only. No
> real tag runs the protocol yet, and the analysis thresholds still need a
> real skate session.

## Install

```sh
cd host
uv venv && uv pip install -e '.[all]'      # or: python -m venv .venv && pip install -e '.[all]'
source .venv/bin/activate
```

`[all]` adds bleak (BLE) and matplotlib (plots). Analysis alone only needs
numpy, scipy and pandas. Use `.[dev]` to also get pytest.

## Recording

```sh
bladetag scan                     # tags in range: state, position, battery
bladetag info [ADDR]              # Info + Status
bladetag position [ADDR] left     # store which foot the tag is on
bladetag record [ADDR] --plot     # record into ./sessions/session-YYYYMMDD-HHMMSS/
bladetag identify [ADDR] 5        # blink the LED
bladetag stop [ADDR]              # stop a recording left running
bladetag sleep [ADDR]             # System OFF
```

`ADDR` can be left out when exactly one tag is in range. `record` stops on
Ctrl-C, after `--duration` seconds, or when the plot window is closed. It
sends STOP and waits for the tag to flush its buffer. If the link drops, it
reconnects and continues the same session without sending START again. If the
tag is already recording when it connects, it joins that session.

Use `fake` as the address to record from a simulated tag in-process, without
any hardware: `bladetag record fake --plot`.

On Linux (BlueZ) the MTU exchange happens on connect. The recorder warns if
the MTU is below 64, because the tag then refuses START.

## Analysis

```sh
bladetag dump SESSION             # raw contents: packets, gaps, clock fit, battery
bladetag analyze SESSION --plot overview.png --raw raw.png --csv strokes.csv
```

`SESSION` is a session folder or its `.zip` export, from either this recorder
or the Android app.

What it does (`src/bladetag/analysis.py`):

1. **Clocks:** fits the true sample rate (`timestamp_us` against
   `sample_index`) and maps tag time onto the receiver clock using the lower
   envelope of the receive delay. Packets that were buffered during a link
   drop don't affect the fit.
2. **Ground contact:** high-frequency acceleration (road vibration, above
   20 Hz) is only present while the wheels are on the road. The threshold is
   found per 60 s block, so it adapts to the surface.
3. **Stroke cycles:** one cycle of the tag's foot runs from one wheel
   set-down to the next. These are cross-checked against |gyro| peaks from
   the recovery swing. If the two disagree (e.g. a damped cuff mount with no
   clear vibration), the gyro peaks are used and contact/air is flagged as
   unreliable.
4. **Metrics per cycle:** period, push rate (2× this foot's cycle rate, since
   feet alternate), contact time, air time, GPS speed, and distance per push
   (speed × period / 2).
5. **Session:** medians, a per-km table, and fatigue drift: the first 20 %
   compared with the last 20 % of the distance (or of the time, without GPS).

The plots: `--plot` shows push rate + speed, distance per push, speed vs push
rate and contact share over the session. `--raw` shows ~10 s of raw signal
with the detected contact phases and cycle starts, to check the detection.
Use `--raw-at SECONDS` to pick the window.

## Simulated data

```sh
bladetag simulate /tmp/sim --duration 1800 --drop 600:20 --drop 1200:60
bladetag analyze /tmp/sim
```

`sim.py` is a rough kinematic model of one skate: glide, push onto the inside
edge, recovery in the air, set-down impact, and road vibration while the
wheels are down. The tag gets a random mount rotation. The model's output is
packed into a real session folder with a skewed sample clock, BLE latency,
link drops (buffered, and overflowing when longer than the buffer) and GPS.
`sim_truth.json` holds the true event times, which the tests compare against.
The model is only meant to exercise the code. Don't tune thresholds on it.

## Code layout

| File | |
|---|---|
| `protocol.py` | BLE protocol codec: advert, Info, Status, Data, Control |
| `session.py` | session folder writer/reader (`.bin` records, `session.json`, `gps.csv`) |
| `timesync.py` | sample-rate and tag→receiver clock fits |
| `link.py` | `TagLink` interface, bleak implementation, scanning |
| `recorder.py` | record loop: START/resume, logging, reconnect, STOP |
| `faketag.py` | in-process fake tag implementing the protocol state machine |
| `sim.py` | synthetic skating sessions |
| `analysis.py`, `plots.py` | analysis v1 and its plots |
| `liveplot.py` | live plot during recording |
| `cli.py` | the `bladetag` command |

```sh
pytest           # protocol vectors, session round-trip, recorder against the fake tag, analysis vs sim truth
```

## Not done yet

- A real skate session, to tune the thresholds (vibration band, low-pass,
  minimum contact/air) and check them on frame vs. cuff mounts.
- Push vs. glide *within* ground contact. This needs the mounting orientation
  and edge angle (phase 9). v1 only splits contact from air.
- Two tags: the recorder handles one tag per run, and the analysis treats
  each tag separately. Left/right balance and timing are phase 8.
