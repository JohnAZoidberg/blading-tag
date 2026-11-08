# host

Python tools that run on a laptop: a BLE recorder with a live plot, and the
session analysis. Phases 4 and 6 in [../PLAN.md](../PLAN.md). They implement
both interfaces in [../PROTOCOL.md](../PROTOCOL.md).

> **Status:** the recorder works with a real tag: every rate from 25 to
> 800 Hz with no gaps, and joining a recording after the recorder crashed.
> Link drops lose no samples: the hardware tests (`tests/hw/`) drop the link
> 20 times with `bluetoothctl disconnect` and 8 times with real supervision
> timeouts. Walking out of range hasn't been tried since then. The analysis
> runs on real recordings, but its thresholds still need a real skate session.

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
bladetag position [ADDR] left_foot  # where it's worn: left_foot, right_wrist, backpack, …
bladetag record [ADDR] --plot     # record into ./sessions/session-YYYYMMDD-HHMMSS/
bladetag identify [ADDR] 5        # blink the LED
bladetag stop [ADDR]              # stop a recording left running
bladetag sleep [ADDR]             # System OFF
```

`ADDR` can be left out when exactly one tag is in range. `record` stops on
Ctrl-C, after `--duration` seconds, or when the plot window is closed. It
sends STOP and waits for the tag to flush its buffer. If the link drops, it
reconnects and continues the same session without sending START again. If the
tag is already recording when it connects, it joins that session. If the tag
stops on its own (button press, or 5 min without a link), the recording ends
there.

Use `fake` as the address to record from a simulated tag in-process, without
any hardware: `bladetag record fake --plot`.

On Linux (BlueZ) the MTU exchange happens on connect. The recorder warns if
the MTU is below 64, because the tag then refuses START.

BlueZ keeps a link up after the program using it dies. The tag then thinks
it's connected: it stops advertising and its data goes nowhere. When the
recorder can't find a tag by scanning but BlueZ still holds a link to it, it
disconnects that link, so the tag buffers and advertises again (this also
takes the tag from any other program on the laptop that's using it). Stop the
recorder with Ctrl-C (SIGINT), not `kill`, so it can send STOP and save.

## Exploring a recording

Before the analysis can be trusted, look at what a real recording actually
contains:

```sh
bladetag explore SESSION                       # summary + zoomable overview window
bladetag explore SESSION --start 300 --end 900 # only part of the session
bladetag explore SESSION --psd 0:5:standing --psd 120:180:walking --psd 400:460:skating
bladetag explore SESSION --html overview.html   # same overview as a page for any browser
bladetag explore SESSION --png overview.png --export samples.csv
bladetag explore SESSION --strides strides.csv # walking: length of every stride
```

It prints a summary: rest periods and which sensor axis points up during
each one, peaks and clipping against the configured range, vibration RMS
above 20 Hz while moving vs. still, the dominant repetition period, and GPS
speed and accuracy.

The overview window shows on one shared time axis: the accel and gyro axes
(a min/max envelope zoomed out, raw samples once you zoom in), an
acceleration spectrogram, a cadence track (autocorrelation of the gyro in
8 s windows, coloured by how repetitive it is), and GPS speed. Rest periods
are shaded grey and gaps red. `--psd` compares the spectra of time ranges,
e.g. walking vs. skating, smooth vs. rough road, frame vs. cuff mount.

`--html` writes the same overview as one self-contained page (samples
embedded, no network or matplotlib needed; about 4 MB for 20 minutes), so it
can be opened on a phone or sent to someone. Scroll to zoom, drag to pan,
shift-drag to zoom to a range, double-click for the whole session; the
legend buttons hide axes, hovering reads out the values, and the URL keeps
the zoomed range (`overview.html#t=230-360`).

**Walking.** When the tag is worn walking, the summary also gives the stride
length (from one plant of the tag's foot to the next; a step is about half
of it), the walking speed, and the distance compared with GPS. The HTML page
gets a step panel and `--strides` writes every stride. It integrates the
IMU between foot plants, resetting the velocity to zero at each one
(`src/bladetag/walking.py`). On the first real walks it agreed with GPS to
1 % where GPS was accurate (±4 m). It only works when the foot actually stops:
gliding skates never do, and their road vibration keeps them from being
mistaken for a planted foot, so skating sessions report no strides.

Note down what you did and when (stand still, 10 easy strides, 10 hard
strides, ...), so the ranges can be found again afterwards. Times are
seconds since the session start, as in `analyze --raw-at`.

In a notebook:

```python
from bladetag.explore import load, cadence_track, psd
df, fs, gps = load("sessions/session-...")   # one row per sample, in g and dps
```

`--export` writes the same table to `.csv` (or `.parquet` if pyarrow is
installed).

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
| `explore.py` | raw-signal exploration: per-sample table, overview, spectra |
| `explore_html.py`, `explore_overview.html` | `explore --html`: the overview as one self-contained page |
| `walking.py` | stride length when walking (zero-velocity updates at foot plants) |
| `jlink.py` | J-Link test hook driver for the hardware tests |
| `analysis.py`, `plots.py` | analysis v1 and its plots |
| `liveplot.py` | live plot during recording |
| `cli.py` | the `bladetag` command |

```sh
pytest           # protocol vectors, session round-trip, recorder against the fake tag, analysis vs sim truth, explore, walking
```

The hardware tests in `tests/hw/` run against a real tag with a J-Link
(`make build-test ota-test test-hw` from the repo root) and are skipped
otherwise. See [tests/hw/README.md](tests/hw/README.md).

## Not done yet

- A real skate session, to tune the thresholds (vibration band, low-pass,
  minimum contact/air) and check them on frame vs. cuff mounts.
- Push vs. glide *within* ground contact. This needs the mounting orientation
  and edge angle (phase 9). v1 only splits contact from air.
- Two tags: the recorder handles one tag per run, and the analysis treats
  each tag separately. Left/right balance and timing are phase 8.
