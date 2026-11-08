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
2. Tick one tag, or two, and press **Start recording**. Stand still for 2 s,
   then skate. The phone can go in your pocket with the screen off. The
   recording runs in a foreground service, and its notification has a Stop
   button. Untick **Record GPS** to record without location. The session
   then has no `gps.csv`, so there is no speed or distance per push. The
   choice is remembered.
3. **Save and start new session** (also in the notification) saves the
   session so far and keeps recording into a new one, to separate movement
   patterns. The tags stay connected and only the files change, so nothing
   is lost at the cut. **Description…** sets a free-text description
   (session.json `notes`) for the session being recorded.
4. **Stop recording** sends STOP, waits for the tag to flush its buffer, and
   saves the session.
5. Under **Sessions**, **Edit** changes a session's description and each
   tag's body location, **Share** zips a session into
   `Downloads/blading-tag/` and opens the share sheet, and **Delete** removes
   it from the phone.

If the link drops mid-session, the app reconnects as soon as the tag is back
in range. The tag buffers in the meantime (PROTOCOL.md §1.9). If the tag is
found READY after a reconnect (for example it rebooted, or was gone for more
than 5 minutes), the app starts it again rather than lose the rest of a long
skate. The new `session_id` is visible in the logged Status records.

Debug builds also list a **Simulated tag**. It produces byte-exact protocol
packets at 200 Hz (with a sample clock that runs 0.4 % fast and a fake
70 strokes/min pattern), so the app, the session files and the analysis can
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
| `GattTag.kt` | The real tag over BLE: connection setup, GATT operation queue, START/STOP, reconnects |
| `SimulatedTag.kt` | The fake tag for debug builds |
| `RecorderService.kt` | Foreground service: owns the links, GPS, files and wake lock |
| `MainActivity.kt` | The one screen: scan, start/stop, live view, sessions |
| `Sessions.kt` | Listing and zip export of recorded sessions |

## Status

- Tested on a Fairphone 6 (Android 16) with the simulated tag: a recording
  with the screen off had no gaps, and the files decode with `host/`'s
  `bladetag.protocol`.
- Records from the real tag: two walks with GPS on 2026-11-07 (tag on the
  ankle), plus Stop on a recording the laptop started. No skate session
  yet.
- Not done yet: live push rate, firmware updates from the app (use nRF
  Connect Device Manager for now), START parameters other than the defaults.
