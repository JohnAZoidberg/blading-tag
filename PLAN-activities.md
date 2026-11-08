# Activities: skating, walking, cycling

Today everything assumes skating: the app says "move the skate", the live
panel shows push rate and distance per push, and `bladetag analyze` looks for
pushes. But the same tag on the foot is also useful for walking (already
used for testing, and `walking.py` measures stride length) and for cycling
(cadence in rpm, and with GPS the distance per crank turn).

The plan: each session records **which activity it is**, and **what gear**
was used (the shoes, the skates or wheels, the bike). The app asks for both
before recording, and the tools pick their metrics and wording from the
activity. The gear makes it possible to compare, say, two pairs of shoes:
see [Comparing gear](#comparing-gear-eg-shoes).

## Activities

| Label | Name in the UI | Main rate | Distance per … (with GPS) | Analysis |
|---|---|---|---|---|
| `skating` | Skating | pushes/min | push | `analysis.py` (exists) |
| `walking` | Walking | steps/min | step (also from the tag alone) | `walking.py` (exists) |
| `cycling` | Cycling | rpm | crank turn ("gear") | new, needs a first ride |
| `unknown` | | | | `explore` only |

Running would be a fourth later; `walking.py` should mostly work for it.

## Where the activity is stored

- **In `session.json`**, as a new top-level field `"activity": "skating"`.
  This is additive, so `format_version` stays 1 (readers ignore unknown
  fields). Missing means `unknown`, which is every session recorded so far.
- **Not on the tag.** The activity belongs to a session, not to the tag, and
  the tag behaves the same for all of them. No firmware or BLE change.
- Editable after recording, like the notes and the body location.
- One session is one activity. For a mixed outing (walk to the trail, then
  skate) use "Save and start new session" at the switch and give each part
  its own activity.
- **`"gear"`**, also top-level: free text naming what was worn or ridden,
  e.g. `"Brooks Ghost 15"`, `"FR 90 mm wheels"`, `"road bike"`. Empty
  (missing) means not given. One field for all activities, so comparing
  skate wheels later works the same way as comparing shoes; the UI calls it
  Shoes, Skates or Bike depending on the activity. Typed names are matched
  as they are, so the app offers the names used before rather than having
  them typed again.

## Android app

1. **Activity picker** at the top of the idle screen (three buttons, one
   selected), remembered in the prefs. It applies to "Start recording" and to
   Standby, so a button-started recording gets the activity picked before
   putting the phone away. Standby's text says which one.
2. **Wording** follows the activity: "Move the skate/foot to wake the tag",
   the GPS checkbox reason, "then skate/walk/ride".
3. **Live panel**: the big number is push rate, steps/min or rpm; the tile
   under it is distance per push, step or crank turn. The live cadence code
   (`LiveSignal`) may need a different period range per activity (cycling is
   0.7–1.8 Hz per foot, faster than skating pushes).
4. **Session list** shows the activity; the edit dialog can change it.
5. "Save and start new session" keeps the activity and the gear; change
   them afterwards in the edit dialog.
6. **Gear** field under the activity picker ("Shoes", "Skates" or "Bike"):
   a text field with the names used before for that activity to pick from,
   remembered per activity. Also in the edit dialog and the session list.

`LivePanel.kt` and `LiveSignal.kt` are being worked on in another session,
so coordinate before step 3.

## Host tools

1. `session.py` reads and writes `activity` (default `unknown`) and `gear`
   (default empty).
2. `bladetag record --activity skating|walking|cycling --gear NAME`.
3. `bladetag analyze` picks the analysis from the activity, with
   `--activity` to override it (needed for all the old sessions). With
   `unknown`, it says so and points to `explore` instead of guessing.
4. `bladetag explore` stays activity-neutral (raw signal, cadence track), but
   only runs the stride section for walking (and for `unknown`, as now).
5. `bladetag label SESSION... [--activity LABEL] [--gear NAME]` to set
   them on sessions after the fact (all the old ones are `unknown`).
6. `bladetag compare SESSION...`: walks side by side, grouped by gear; see
   below.

## Comparing gear (e.g. shoes)

How do different shoes change the way I walk? The tag on the shoe sees each
stride, and `walking.py` already measures, per stride (done, cc45121):

| Measure | What it shows | Two walks in the same shoes (2026-11-07) |
|---|---|---|
| stride length, period | how far and how fast each step goes | 1.50 / 1.49 m |
| stance % | share of the stride with the foot on the ground | 55.7 / 56.3 % |
| strike ° | foot pitch at heel strike: heel-first (+20°…) vs. flat or forefoot (≤ 0) | +24.8 / +25.6° |
| toe-off ° | how far the foot rolls over the toes before lifting | −59.7 / −58.6° |
| impact g | how hard the landing is (cushioning) | 4.44 / 4.37 g |
| lift cm | how high the foot swings | 15.5 / 15.2 cm |
| swing dps | how fast the foot turns in the swing | 416 / 408 dps |

(Both walks at 4.9 km/h, tag on the right ankle.)

Nearly all of these change with walking speed, so `walking.compare()`
compares walks at one common speed: it takes the strides within 1 km/h of
it and shifts each measure along its fit against speed. Without that, a
faster walk in one pair would look like a different gait.

The unit to compare is a **walk**, not a stride: hundreds of strides from
one walk share the day, the route and the legs' state, so their spread
says nothing about whether the next walk would come out the same. The
table shows the spread between walks in the same shoes; a difference
between shoes means something only if it's clearly bigger than that.

**How to record a comparison:**

- Wear the tag on the shoe (on the laces, or behind the heel), in the same
  spot on every pair. Its angle doesn't matter (each stride is levelled at
  the foot plant) but its place does: the heel lands harder than the laces.
  On the shin the angles are the shin's, still fine for a comparison as
  long as it's the same on every walk.
- Same route, flat, at a steady normal pace, 15–20 min of walking each.
  Stops are fine; they're skipped.
- At least 2–3 walks per pair, alternating (A, B, A, B), so a good or bad
  day doesn't land on one pair only.
- Set the activity to Walking and type the shoes' name before starting.

**Next steps:**

- [x] `bladetag compare SESSION... [--by gear|session|activity]
      [--speed KMH] [--csv FILE]`; groups by `gear`, falling back to the
      folder name.
- [ ] Same comparison in `explore --html`, or a small HTML report: each
      measure against speed, one colour per pair, so it's visible whether
      the shoes differ at all speeds or only at some.
- [ ] Maybe later, once the above works on real shoe walks:
  - **Roll** (pronation): the foot's rotation about its long axis from heel
    strike to foot flat. Needs the side (left/right) for the sign, and the
    tag on the shoe, not the shin.
  - **Fatigue**: the same measures over the first and the last 10 min of a
    long walk, per pair.
  - **Left vs. right**, with a second tag.

## Cycling analysis

Waits for a first recorded ride. What to record: 20–30 min with GPS, with
steady seated riding at two or three cadences, a few gear changes at the
same cadence, some coasting, a standing climb and a stop. Note roughly when
each happened. A bike computer's cadence sensor at the same time gives a
reference to check against.

Expected metrics, roughly in order of how sure we are they work:

1. **Cadence** (rpm), over the ride and its drift with fatigue.
   `explore.cadence_track` already covers 20–200 rpm.
2. **Pedalling vs. coasting** time.
3. **Distance per crank turn** = GPS speed ÷ cadence, i.e. the gear. Shows
   gear changes and time in each gear without a sensor on the bike.
4. **Standing vs. seated**, if the foot's sway and tilt differ enough.
5. **Technique**: ankle angle through the turn, crank speed variation within
   a turn.
6. **Not power.** A rough estimate from speed, climb (BME688 pressure) and a
   bike and rider model is a "maybe".

The first ride uses the current settings (200 Hz, ±16 g, ±2000 °/s). If
cycling only needs cadence, 100 Hz would save battery on long rides; decide
that from the data.

## Order of work

- [x] **1. Protocol**: document `activity` and `gear` in PROTOCOL.md §2.2.
- [x] **2. Host**: read/write `activity` and `gear`, `record --activity
      --gear`, `bladetag label`, `bladetag compare`, `analyze` dispatch and
      `--activity`.
- [x] **3. Android**: activity picker and gear field, session.json fields,
      edit dialog, session list, wording.
- [x] **4. Android live panel**: per-activity big number and
      distance-per tile. Pushes/min, steps/min or rpm (cycles of the
      tagged foot ×2, ×2, ×1), metres per push/step/crank turn, also in the
      chart title and the notification. Unknown shows cycles/min of one
      foot. The period range (0.3–3 s) already covers cycling, so the
      detector is the same for all three. Checked on the phone with the
      simulated tag as Cycling (40 rpm).
- [ ] **5. First cycling ride**, then `cycling.py`: cadence,
      pedalling/coasting, distance per turn; then decide on the rest.

## Open questions

- Should the app guess the activity instead of asking? Walking has a still
  foot every stride, skating has road vibration while gliding, cycling has a
  smooth circle with neither. A guess could fill in `unknown` sessions, or
  warn when the picked activity looks wrong. Start with asking; revisit once
  there's data for all three.
- Does the "2 s standing still" at the start make sense on a bike? Probably
  "stand with the foot on the ground" works, but check on the first ride.
