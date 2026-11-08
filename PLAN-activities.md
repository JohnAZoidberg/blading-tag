# Activities: skating, walking, cycling

Today everything assumes skating: the app says "move the skate", the live
panel shows push rate and distance per push, and `bladetag analyze` looks for
pushes. But the same tag on the foot is also useful for walking (already
used for testing, and `walking.py` measures stride length) and for cycling
(cadence in rpm, and with GPS the distance per crank turn).

The plan: each session records **which activity it is**. The app asks for it
before recording, and the tools pick their metrics and wording from it.

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
5. "Save and start new session" keeps the activity; change it afterwards in
   the edit dialog.

`LivePanel.kt` and `LiveSignal.kt` are being worked on in another session,
so coordinate before step 3.

## Host tools

1. `session.py` reads and writes `activity` (default `unknown`).
2. `bladetag record --activity skating|walking|cycling`.
3. `bladetag analyze` picks the analysis from the activity, with
   `--activity` to override it (needed for all the old sessions). With
   `unknown`, it says so and points to `explore` instead of guessing.
4. `bladetag explore` stays activity-neutral (raw signal, cadence track), but
   only runs the stride section for walking (and for `unknown`, as now).
5. A `bladetag activity SESSION LABEL` command to set it on old sessions.

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

- [ ] **1. Protocol**: document `activity` in PROTOCOL.md §2.2.
- [ ] **2. Host**: read/write `activity`, `record --activity`,
      `bladetag activity`, `analyze` dispatch and `--activity`.
- [ ] **3. Android**: picker, session.json field, edit dialog, session list,
      wording.
- [ ] **4. Android live panel**: per-activity big number and
      distance-per tile (coordinate with the live-panel session).
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
