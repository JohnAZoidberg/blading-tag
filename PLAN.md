# blading-tag: plan

Strap a Nordic nRF54L15 Tag to an inline skate, record how the skate moves, and
analyse it, starting with long-distance skating metrics.

## Goals

In priority order:

1. **Push rate**: strokes per minute, live and over the whole session.
2. **Efficiency**: distance per push (GPS speed ÷ push rate), how much of each
   stroke is glide vs. push, and speed vs. push-rate curves.
3. **Fatigue drift**: how push rate, distance per push and left/right balance
   change from the first km to the last.
4. **Later, technique**: edge angle, push direction, and crossovers in turns.

Sessions are long (2–3+ h), so battery life and reliable logging matter more
than anything that only fits short bursts.

## Hardware

[nRF54L15 Tag](https://www.nordicsemi.com/Products/Development-hardware/nRF54L15-Tag).
The Zephyr board target is `nrf54l15tag/nrf54l15/cpuapp`, and it is already in
NCS v3.4.0, so no custom board definition is needed.

| Part | Bus | Role in this project |
|---|---|---|
| nRF54L15 (1.5 MB NVM, 256 kB RAM) | | Main chip |
| BMI270 6-axis IMU | SPI22, CS P1.07, INT P1.04 | Main sensor: accelerometer + gyroscope |
| ADXL367 accelerometer | I2C21 @0x1d | Wake-on-motion while the tag is asleep |
| BME688 environment sensor | I2C21 @0x76 | Maybe later: barometric altitude for climbs |
| RGB LED, 1 button | GPIO | Status, manual start/stop |
| CR2032 coin cell | | Power: keep under ~3 mA continuous |

The sensor nodes are `status = "disabled"` in `nrf54l15tag_common.dtsi`, but
`nrf54l15tag_cpuapp_common.dtsi` enables all three for the cpuapp target.

Factory state (read over BLE on 2026-11-07): it advertises as `Nordic_LBS` and
already has the SMP (mcumgr) DFU service. Slot 0 holds version `3.2.99`,
confirmed, with a SHA-512 hash (ED25519 / NCS nRF54L signing).

The factory image is sdk-nrf's `samples/bluetooth/peripheral_lbs`, test
`sample.bluetooth.peripheral_lbs.l15_tag` (added 2026-01-20, when the sample's
VERSION was 3.2.99). That test only sets `SB_CONFIG_BOOTLOADER_MCUBOOT=y` and
`CONFIG_NCS_SAMPLE_MCUMGR_BT_OTA_DFU=y` plus RTT logging. Its MCUboot uses
the public dev key `bootloader/mcuboot/root-ed25519.pem` (ED25519, SHA-512),
no KMU, swap-using-move (SMP bootloader info reports mode 3), and no downgrade
prevention. All of this is verified: our image signed with that key boots.

**The factory layout is not NCS 3.4's default.** It is the "FLPR" variant of
`nrf54l15_cpuapp_partition.dtsi` (`WITH_FLPR_PARTITIONS`). This was read back
from a stock tag's RRAM over SWD on 2026-11-07:

| Partition | Factory | NCS 3.4 default |
|---|---|---|
| mcuboot | 0x000000, 62 KiB | same |
| slot0 | 0x010000, 664 KiB | 0x010000, 712 KiB |
| slot1 | 0x0b6000, 664 KiB | 0x0c2000, 712 KiB |
| storage | 0x15c000, 36 KiB | 0x174000, 36 KiB |

With the default layout, the image still boots, but it writes its "confirmed"
flag past the end of MCUboot's slot0, so MCUboot reverts it on the next reset.
`firmware/boards/nrf54l15tag_nrf54l15_cpuapp.overlay` sets the factory layout
for the app and, through `firmware/sysbuild/mcuboot.overlay`, for our MCUboot.

A J-Link is available and plugs straight into the Tag's debug header. The tag
is not powered through that header, so the coin cell must be in. Never put
3.3 V on the Tag's VDD while the cell is in. It is only for recovery: tags
are updated over BLE, starting with the very first update from the factory
firmware.

## Architecture

```
 [tag on skate] --BLE--> [Android app, in pocket] --file--> [laptop: Python analysis]
  BMI270 @200 Hz          foreground service                 orientation, push detection,
  batched notifications   raw IMU + GPS log                  metrics, plots
```

- **Mounting:** fixed rigidly to the skate, on the frame (cleanest signal) or
  the cuff (easier). Mount it the same way every session. The recorder asks
  for ~2 s of standing still at the start to find "down".
- **Streaming to the phone instead of storing on the tag:** 2 h of raw data is
  ~17 MB, more than the tag can hold, and the phone adds GPS and a reference
  clock.
- **Second tag (later):** one per foot, for left/right balance and timing
  between the feet. The phone records (tag clock, phone clock) pairs
  regularly and fits drift, which puts both tags on one timeline to within a
  few milliseconds.

## Power states

The tag records **only when the app asks it to** (or when the button is
pressed). The rest of the time it is idle.

| State | What's running | Approx. current | One CR2032 lasts |
|---|---|---|---|
| Recording | BMI270 200 Hz + BLE streaming | ~1–1.5 mA | tens of hours |
| Ready | Connectable advertising at ~1 s, IMU suspended | ~5–10 µA | ~2–3 years |
| Asleep | System OFF, ADXL367 motion wake-up only | ~1 µA | beyond the cell's shelf life |

Transitions:

- Asleep → Ready: ADXL367 sees motion, or the button is pressed.
- Ready → Recording: the app writes "start", or the button is pressed.
- Recording → Ready: the app writes "stop", or the link has been lost for
  longer than a timeout.
- Ready → Asleep: ~10 min with no motion and no connection.

These are estimates from the datasheets and Nordic's tag blog post. Measure
them later with a PPK2 (cut SB1, measure across TP6/TP7).

TX power is +7 dBm, the most the tag's QFN48 nRF54L15 can do, for range from
the ankle to a phone on the body. Against 0 dBm that adds roughly 5 mA while
the radio transmits: ~+60 µA while recording (~1.4 % TX duty) and ~+5 µA
while advertising in READY. That's cheap because the tag sleeps after 10 min
idle (`BLADING_IDLE_SLEEP_S`). The ~10 mA TX peaks are harder on an
old or cold coin cell.

The factory firmware is not power-optimised, so take the coin cell out of a
tag that still runs it. Our firmware sleeps on its own after 10 min idle.

## Interfaces

Firmware and software are developed separately. They only meet at two
interfaces, both specified in [PROTOCOL.md](PROTOCOL.md):

1. **BLE protocol** (tag ↔ Android app / laptop recorder): advertising,
   the Blading GATT service (Info, Control, Status, Data), states, commands,
   the IMU packet format, time sync and link-loss behaviour.
2. **Session file format** (recorders ↔ analysis): `session.json`, raw
   notification logs per tag, `gps.csv`.

Changes to either one go into PROTOCOL.md first, following its versioning
rules. Then the two sides are updated.

## Build and flash

Built the same way as `../hid-remapper-private`:

- The container is `ghcr.io/nrfconnect/sdk-nrf-toolchain:v3.4.0` (podman), so
  nothing needs to be installed on the host.
- `firmware/west.yml` pins NCS v3.4.0. The west workspace (`zephyr/`, `nrf/`,
  `modules/`, `bootloader/`, …) is cloned into the repo root and gitignored.
- `Makefile` targets:
  - `make build`: pristine sysbuild of MCUboot + app
  - `make ota`: `host/ota.py` (uv script, smpclient). It uploads in test
    mode, resets, and checks that the new image runs and confirmed itself
  - `make image-state`, `make reset`: show the image slots, reset over SMP
  - `make flash`: `nrfutil` over J-Link, MCUboot (erasing the chip) and then
    the signed app (recovery only)
  - `make recover`: unlock and erase a tag whose debug port is protected
  - `make build-test ota-test test-hw`: hardware tests, with firmware that
    has the J-Link test hook (`firmware/test.conf`); see firmware/README.md
- **OTA from the phone:** nRF Connect Device Manager with `dfu_application.zip`.
- **OTA config:**
  - `prj.conf`: `CONFIG_NCS_SAMPLE_MCUMGR_BT_OTA_DFU=y`; the app calls
    `boot_write_img_confirmed()` once Bluetooth is up
  - `sysbuild.conf`: `SB_CONFIG_BOOTLOADER_MCUBOOT=y`,
    `SB_CONFIG_MCUBOOT_MODE_SWAP_USING_MOVE=y`
  - the factory partition layout (above), in the board overlay

**First OTA onto the factory image works without pairing.** The factory SMP
characteristic accepted unauthenticated writes from both BlueZ and smpclient.
Tested on 2026-11-07: factory 3.2.99 → 0.1.0 → 0.1.0+1 → 0.1.0 with `make ota`, each
step confirmed and still running after a reset, with the previous image kept in
slot 1. Our own MCUboot is only flashed when recovering with the J-Link. A
tag updated over BLE keeps the factory MCUboot, which is fine because it uses
the same key and layout.

Images are signed with MCUboot's default debug key, so they are for
development only.

## Repo layout

```
firmware/     Zephyr app for the tag (west manifest, prj.conf, overlays, sysbuild/)
android/      Kotlin app: connects to the tag(s), records IMU + GPS (phase 5)
host/         Laptop Python tools: BLE recorder, live plot, analysis (bleak, numpy, pandas)
Makefile      container build, OTA, J-Link flash
README.md     short overview and how to use it
PROTOCOL.md   BLE protocol + session file format (the contract between the parts)
PLAN.md       this file
```

Each subfolder has its own `README.md` covering how to build and use that part.

The west workspace (`zephyr/`, `nrf/`, `modules/`, `bootloader/`, `nrfxlib/`,
`test/`, `.west/`) is cloned into the repo root and gitignored. Host tools stay
in `host/` so they don't collide with NCS checkout paths such as `tools/`.

## Phases

- [x] **1. Repo setup**: `west.yml`, Makefile, `.gitignore`, west workspace
      in the container.
- [x] **2. Canary firmware**: BLE + SMP OTA + LED blink only. OTA it to the
      tag, confirm it boots, then OTA it again to prove updates still work.
- [x] **3. Firmware v1**: BMI270 streaming over the protocol above, start/stop
      from the app or button, LED status. Version 0.2.0. Tested on the tag
      with the host recorder: 30 s at 200 Hz, continuous `sample_index` and
      `seq`, 0 dropped, clock fit ±0.72 ms.
- [ ] **4. Laptop recorder**: `host/` script (bleak). Logs to a file and plots
      live. Do one real skate session to look at the raw signal.
      *Built and recording from the real tag. Still to do: the skate session.*
- [ ] **5. Android logger**: Kotlin foreground service. Connects, starts and
      stops recording, logs IMU + GPS, exports files. First test the stream
      with nRF Connect.
      *Built and recording from the real tag: two walks with GPS on
      2026-11-07, tag on the ankle. Still to do: a skate session.*
- [ ] **6. Analysis v1**: push detection and push rate, push/glide split,
      distance per push, fatigue drift over the session.
      *Built and checked on simulated sessions. Still to do: tune the
      thresholds on a real session.*
- [ ] **7. Power states**: ADXL367 wake-on-motion, System OFF, ready-mode
      advertising, then measure.
      *Working on the tag: 1 s ready advertising, System OFF from SLEEP, a
      2 s button hold or 10 min idle, woken by ADXL367 motion or the button.
      Still to do: measure with a PPK2.*
- [ ] **8. Second tag**: one per foot, clock sync, left/right metrics.
- [ ] **9. Technique**: orientation fusion (Madgwick), edge angle, push
      direction, crossovers.
- [ ] **Maybe**: live audio cues (push-rate beep), push detection on the tag,
      BME688 climb detection, phone-side OTA in our own app.

## Open questions

- Is 200 Hz enough, or does push onset need more? Check against the raw data
  from phase 4.
- Frame vs. cuff mount: compare both in an early session.
- **When should a new image confirm itself?** Today `main.c` confirms as soon
  as Bluetooth is up. An image that gets that far but then can't be reached
  (no advertising, broken connections) is kept, and only a J-Link can fix it.
  That happened once, with the advertising restart loop fixed in c6902e3.
  Proposal: confirm on the first BLE connection instead, which proves a host
  can reach the tag. `host/ota.py` already reconnects after the reset and
  waits for "confirmed", so the update flow doesn't change. ("Advertising
  started" isn't enough: in c6902e3 it started and then kept restarting.)
  Cost: if the tag resets or sleeps before anyone connects, the update is
  lost. Waking from System OFF is a reset, so idle sleep and the 2 s button
  hold would have to wait (or warn) while the image isn't confirmed.

Settled:

- **Data-ready vs. FIFO:** the Zephyr BMI270 driver only has data-ready
  triggers, so the app uses the driver for init and config upload, then runs
  the BMI270 FIFO itself with a ~100 ms watermark interrupt (`imu.c`). The
  CPU wakes ~10 times a second instead of 200.
