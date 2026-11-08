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
bladetag record [ADDR] -a walking -g "Brooks Ghost 15"   # with the activity and the gear (shoes, skates, bike)
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
of it), the walking speed, the distance compared with GPS, and the gait:
the share of the stride with the foot on the ground, the foot's pitch at
heel strike and toe-off, how hard it lands and how high it swings. The HTML page
gets a step panel and `--strides` writes every stride. It integrates the
IMU between foot plants, resetting the velocity to zero at each one
(`src/bladetag/walking.py`). On the first real walks it agreed with GPS to
1 % where GPS was accurate (±4 m). It only works when the foot actually stops:
gliding skates never do, and their road vibration keeps them from being
mistaken for a planted foot, so skating sessions report no strides. Only
sessions marked walking (or not marked at all) get the stride section.

**Comparing shoes.** Mark the walks with the shoes they were done in, then
put them side by side:

```sh
bladetag label sessions/session-20261109-* --activity walking --gear "Brooks Ghost 15"
bladetag compare sessions/session-2026111*          # grouped by gear
bladetag compare SESSIONS... --speed 5 --csv walks.csv
```

`compare` gives stride length, stance share, foot angle at heel strike and
toe-off, impact and lift for each walk at one common speed (strides within
1 km/h of it, each measure shifted along its fit against speed, since nearly
everything in a stride changes with pace), the mean per pair of shoes, and
the spread between walks in the same pair. A difference between shoes only
means something if it's clearly bigger than that spread: record 2–3 walks
per pair on the same route, alternating, with the tag in the same spot on
the shoe. See [PLAN-activities.md](../PLAN-activities.md#comparing-gear-eg-shoes).

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

`analyze` follows the session's activity: skating gets the push analysis
below, walking the stride and gait summary (`--csv` writes every stride).
Sessions recorded before the activity existed are `unknown`; give
`--activity skating` or mark them with `bladetag label SESSION --activity
skating`. There's no cycling analysis yet.

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

## 3D replay of the skate

```sh
bladetag viz3d sessions/session-20261108-1* -o strokes3d.html
```

Writes one page with every skating session given: a 3D skate (three 110 mm
wheels, the tag on the boot) replaying each stroke at ¼ speed, a timeline of
GPS speed with a dot per stroke (sideways sweep, inside edge, toe out, push
rate, distance per push or lift), and the typical stroke from the first 20 %
of the session drawn over the one from the last 20 %, with a table of the
changes. Views: *skater frame* (the skate relative to you, as a camera riding
alongside sees it), *over ground* (the road streams past at the real speed)
and *typical stroke*. Space plays and pauses, ←/→ jump a stroke, and clicking
the timeline jumps there. three.js and the fonts load from a CDN.

How it works (`src/bladetag/viz3d.py`):

1. **Orientation:** a Mahony filter on gyro and low-passed accelerometer,
   then a tilt correction so the world-frame acceleration averaged over 3 s
   is vertical. Pushing biases the plain filter by ~15°.
2. **Path per stroke** (set-down to set-down, from the analysis above): the
   acceleration is integrated twice. Horizontally the stroke is taken as
   periodic and the skater's own speed is removed. Vertically only the air
   phase is integrated; the wheels are on the road otherwise.
3. **Skater frame:** the path's long axis is sideways, and the way the skate
   moves while its wheels are down is outward (which foot the tag is on gives
   the sign). This also gives the tag's mount on the skate, with no setup:
   on 4 sessions every stroke agreed to 0.97 or better.
4. **Wheel check:** wheels only roll along the skate, so while they're down
   the skate points where it's going (GPS speed plus its own sideways
   motion). That calibrates the skate's yaw on the tag. On the first skate
   (2026-11-08) it corrected +10° on the left foot and −12° on the right,
   and the skate's yaw then follows the direction of travel with r ≈ 0.7.

`viz3d.stroke_motion(sess)` gives the same numbers per stroke as a table
(`stroke` = row in `analyze().strokes`): sideways sweep, outward reach,
fore-aft range, lift, inside edge, toe out and peak outward speed. On the
first skate: sweep 0.7–1 m (it includes body sway), inside edge 30–38°, toe
out ~12°, and lift only 1 cm or less. The simulator's 7 cm lift comes out at
6.4 cm, so the recovery really does stay that low.

## Comparing skating sessions

```sh
bladetag report sessions/session-20261108-1* -o report.html --title "Riverside, 8 Nov"
bladetag report sessions -o all.html --csv strokes.csv   # every session in the folder labelled skating
```

Measures every stroke of every skating session given (`src/bladetag/skating.py`)
and writes one self-contained page (`report.py`, `report.html`; data
embedded, works offline) with:

- **Findings in words**: out and back on the same road compared, start vs end
  of the longest session, soft or hard strides, and what to change in the next
  recording to answer what this one couldn't (e.g. foot and direction
  confounded). Then *quick answers*: does speed go with push rate, with
  distance per push, with a wider or harder push, and so on, each with a
  button that plots it.
- **Any measure against any other**: one dot per stroke, a fit and Spearman r
  per session, optionally with each session's median removed.
- **Correlation matrix**, *within a session* (each session's median removed
  first: what a stroke-to-stroke change does) or *pooled* (also picks up
  whatever differed between sessions: wind, foot, fatigue, road). Click a
  cell to plot it.
- **Along the way**: any measure over the whole outing, sessions in order.
- **Map**: strokes coloured by that measure, set off to the right of the
  direction of travel, so out and back sit side by side like lanes.
- **Same place, compared**: strokes from two sessions within 15 m of each
  other, going the same or opposite ways. Same road and slope, so what's left
  is wind, tiredness, the foot and technique.
- **At one common speed**: each session's medians moved along their
  within-session fit against speed, since speed changes nearly everything.
- **Coasting**: speed lost per second while not pushing, which is where wind
  shows up most directly.

Per stroke, on top of the analysis above: GPS speed, heading, position and
speed change; **set-down impact** (peak of the 15 Hz low-passed |a| just after
the wheels land, so road vibration doesn't count); **push-off peak** (the
same at the end of the contact: the kick, the hardest moment of the stroke);
**swing speed** (peak rotation rate in the air); **road vibration** (RMS above
20 Hz while rolling); and the foot path from `viz3d.stroke_motion` (outward
reach, sideways sweep, push speed, edge, toe out, fore-aft).

The equal-effort wind estimate in the out-and-back finding assumes the same
power both ways, an 80 kg skater, CdA 0.4 m² and Crr 0.006 (`report.RIDER`).
It's a lower bound when the slow way also took harder pushes.

What the first out-and-back (2026-11-08, 1137 strokes, tag on the left foot
out and on the right foot back) showed:

- Speed went with distance per push (r = +0.81 within a session), not with push
  rate (+0.03). Faster strokes were longer, not quicker.
- A quicker cadence came with a narrower push (outward reach r = −0.86) and
  a harder kick (+0.38). Neither the kick (−0.03) nor the landing (−0.06)
  changed the distance per push; a faster outward push speed did (+0.53).
- Landings were soft (1.8 g median); the push-off kick was about 6 g.
- Coming back on the same 2.2 km: 17 % slower at 4 % lower push rate, with
  19 % wider pushes and 19 % harder kicks. The speed alone means at least
  ~3 km/h of wind. The tag changed feet at the turn, so left vs right can't be
  separated from the direction; next time keep one foot for a whole
  out-and-back.

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
| `walking.py` | strides and gait when walking (zero-velocity updates at foot plants), `compare` |
| `jlink.py` | J-Link test hook driver for the hardware tests |
| `analysis.py`, `plots.py` | analysis v1 and its plots |
| `skating.py` | skating measures per stroke (impact, push-off, GPS), correlations, speed matching, same-place pairs, coasts |
| `report.py`, `report.html` | `report`: several skating sessions on one page, findings in words |
| `viz3d.py`, `viz3d_template.html` | `viz3d`: skate path and angles per stroke, the 3D replay page |
| `liveplot.py` | live plot during recording |
| `cli.py` | the `bladetag` command |

```sh
pytest           # protocol vectors, session round-trip, recorder against the fake tag, analysis vs sim truth, explore, walking, viz3d, report
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
