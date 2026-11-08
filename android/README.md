# android

Kotlin app that records a skate session: it connects to one or two tags,
starts and stops their recording, and logs the raw IMU stream plus phone GPS
in the session format from [../PROTOCOL.md](../PROTOCOL.md) §2. Phase 5 in
[../PLAN.md](../PLAN.md).

Plain Android SDK, no AndroidX or other dependencies, minSdk 33 (Android 13).

## Using it

1. Wake the tag by moving the skate. It shows up under **Tags** with its
   state, body location and battery. **Blink** flashes its LED so you can
   tell two tags apart, and **Body location…** stores where the tag is worn
   (foot, wrist, pants pocket, backpack) on the tag.
   **Stop** ends a recording that something else started (another phone,
   the laptop, or the tag's button) and drops what the tag still buffers.
   **Sleep** puts a tag that isn't recording into System OFF. It stops
   advertising and drops off the list until moving the tag or pressing its
   button wakes it (see [../firmware/README.md](../firmware/README.md#power-states-status)).
2. Tags in range are ticked as they show up (at most two, preferring the
   ones used last time), so usually you just press **Start recording**.
   Tick or untick to choose others. Stand still for 2 s,
   then skate. The phone can go in your pocket with the screen off. The
   recording runs in a foreground service, and its notification has a Stop
   button. Untick **Record GPS** to record without location. The session
   then has no `gps.csv`, so there is no speed or distance per push. The
   choice is remembered.
3. While recording, the screen is made for a quick look out of the pocket:
   push rate as the big number, then speed, distance and metres per push,
   then the session so far as two charts (push rate and speed), and each
   tag's last 10 s of gyro (tap the plot for accel). The notification shows
   push rate, speed and distance too. See [Live push rate](#live-push-rate).
4. **Save and start new session** (also in the notification) saves the
   session so far and keeps recording into a new one, to separate movement
   patterns. The tags stay connected and only the files change, so nothing
   is lost at the cut. **Description…** sets a free-text description
   (session.json `notes`) for the session being recorded.
5. **Stop recording** sends STOP, waits for the tag to flush its buffer, and
   saves the session. Pressing a tag's button also stops it; the app notices
   and ends the recording (stopping the other tag too).
6. **Standby** (instead of Start recording) is for starting from the tag
   with the phone in a pocket. It waits in a foreground service, with a
   filtered BLE scan, for one of the ticked tags to advertise RECORDING.
   Then it records from all ticked tags (starting the other one) with the
   same GPS setting, picking up the pressed tag's buffer. Once the phone has
   a tag's data, it blinks the tag blue for 2 s. A button press that stops a
   tag ends the session, and the app goes back to waiting. **Leave standby**
   (in the app or the notification) ends it. Arm it while the app is open:
   Android only lets a foreground service use GPS from the background if it
   was started from the foreground.
7. Under **Sessions**, **Edit** changes a session's description and each
   tag's body location, **Share** zips a session into
   `Downloads/blading-tag/` and opens the share sheet, and **Delete** removes
   it from the phone. Sessions from the simulated tag say **SIMULATED**
   (`"simulated": true` in session.json), so they aren't mistaken for real
   activities.

If the link drops mid-session, the app reconnects as soon as the tag is back
in range. The tag buffers in the meantime (PROTOCOL.md §1.9). If the tag is
found READY after a reconnect (for example it rebooted, or was gone for more
than 5 minutes), the app starts it again rather than lose the rest of a long
skate. The new `session_id` is visible in the logged Status records.

### Live push rate

Push rate is computed on the phone once a second, with the same method as
`cadence_track` in `bladetag explore`: the autocorrelation of the gyro vector
over the last 8 s, decimated to 50 Hz. It doesn't depend on how the tag is
mounted and has no thresholds to tune. The repetition period is one stroke
of the foot the tag is on, so pushes/min is twice the strokes of that foot
(skating alternates feet). It's shown only while moving (gyro RMS > 30 °/s)
and repetitive (autocorrelation > 0.5), so standing or rolling without
pushing shows "–". With two tags it's their mean. Distance integrates GPS
speed between fixes, like the host analysis; metres per push is speed
divided by push rate.

To check the port against the host on a real recording:

```sh
cd ../host && .venv/bin/python live_parity.py ../sessions/<session> /tmp/parity.csv
cd ../android && LIVE_PARITY_CSV=/tmp/parity.csv ./gradlew :app:testDebugUnitTest --tests '*matchesHostCadenceTrack' -i
```

On the two 2026-11-07 walks, every repetitive window agreed with the host
within 5 % (445 of 445 and 102 of 102).

### Simulated tag

Debug builds also list a **Simulated tag**. It produces byte-exact protocol
packets at 200 Hz (with a sample clock that runs 0.4 % fast and a fake
stroke pattern at 60-80 pushes/min that drifts slowly), so the app, the session files and the analysis can
be tested without hardware. Debug builds also show over the lock screen, so
the phone can be driven over adb without unlocking it.

## Building

Needs the Android SDK (platform 36) in `~/Android/Sdk`, and any JDK 17+.
Gradle downloads a compiler toolchain if needed.

```sh
make test     # unit tests: PROTOCOL.md hex vectors, .bin/.json writers
make flash    # build, install and launch on the phone
make pull     # copy sessions from the phone to ../sessions/
make help     # everything else: log, crash, screenshot, wireless pairing
```

Sessions are stored in `Android/data/app.bladingtag/files/sessions/` on the
phone, which `make pull` reads over adb.

## Code

| File | What it does |
|---|---|
| `Protocol.kt` | UUIDs, Info/Status/Data/advertising parsers, Control commands |
| `SessionFiles.kt` | `tag-*.bin`, `gps.csv` and `session.json` writers |
| `TagLink.kt` | Base for one tag in a recording: logs payloads verbatim, keeps live counters (gaps, measured rate) |
| `LiveSignal.kt` | The last 30 s of a tag's motion at 50 Hz, push rate by autocorrelation, the per-second trend |
| `LivePanel.kt` | Top of the recording screen: push rate, speed, distance, trend charts, motion plots |
| `PlotView.kt` | Small Canvas line chart used by the live panel |
| `GattTag.kt` | The real tag over BLE: connection setup, GATT operation queue, START/STOP, reconnects |
| `SimulatedTag.kt` | The fake tag for debug builds |
| `RecorderService.kt` | Foreground service: owns the links, GPS, files and wake lock; standby scan |
| `MainActivity.kt` | The one screen: scan, start/stop, live view, sessions |
| `Sessions.kt` | Listing and zip export of recorded sessions |

## Status

- Tested on a Fairphone 6 (Android 16) with the simulated tag: a recording
  with the screen off had no gaps, and the files decode with `host/`'s
  `bladetag.protocol`.
- Records from the real tag: two walks with GPS on 2026-11-07 (tag on the
  ankle), plus Stop on a recording the laptop started. No skate session
  yet.
- Live push rate, trend charts and motion plots: checked on the phone with
  the simulated tag, and the push-rate method against the host on the real
  walks. Not yet seen on a skate session.
- Not done yet: firmware updates from the app (use nRF Connect Device
  Manager for now), START parameters other than the defaults.
