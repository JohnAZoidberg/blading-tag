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

All three sensors are `status = "disabled"` in the board DTS, so the app
overlay enables them.

Factory state (read over BLE on 2026-11-07): it advertises as `Nordic_LBS` and
already has the SMP (mcumgr) DFU service. Slot 0 holds version `3.2.99`,
confirmed, with a SHA-512 hash (ED25519 / NCS nRF54L signing).

The factory image is sdk-nrf's `samples/bluetooth/peripheral_lbs`, test
`sample.bluetooth.peripheral_lbs.l15_tag` (added 2026-01-20, when the sample's
VERSION was 3.2.99). That test only sets `SB_CONFIG_BOOTLOADER_MCUBOOT=y` and
`CONFIG_NCS_SAMPLE_MCUMGR_BT_OTA_DFU=y` plus RTT logging, so the factory
MCUboot uses NCS defaults: ED25519 (hash, not pure), the public dev key
`bootloader/mcuboot/root-ed25519.pem`, no KMU, swap-using-move, no downgrade
prevention, and the default nRF54L15 layout (slot0 0x10000, slot1 0xc2000,
712 KiB each). Our build uses the same settings.

A J-Link is available and plugs straight into the Tag's debug header. The tag
is not powered through that header, so the coin cell must be in. Never put
3.3 V on the Tag's VDD while the cell is in. The J-Link does the first flash
(see "Build and flash"), and after that it is only for recovery.

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

The factory firmware is not power-optimised. Take the coin cell out between
uses until our firmware is on the tag.

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
  - `make ota`: `smpmgr --ble <addr> upgrade` from the laptop, uploaded in
    test mode
  - `make flash`: `nrfutil` over J-Link (recovery only)
- **OTA from the phone:** nRF Connect Device Manager with `dfu_application.zip`.
- **OTA config,** per Nordic's tag blog post:
  - `CONFIG_BOOTLOADER_MCUBOOT=y`, `CONFIG_NCS_SAMPLE_MCUMGR_BT_OTA_DFU=y`
  - `sysbuild.conf`: `SB_CONFIG_BOOTLOADER_MCUBOOT=y`
  - `sysbuild/mcuboot.conf`: `CONFIG_FPROTECT_ALLOW_COMBINED_REGIONS=y`,
    `CONFIG_PM_PARTITION_SIZE_MCUBOOT=0xF800`

**First OTA onto the factory image:** the key and layout match (see above).
The obstacle is pairing. The LBS sample's `CONFIG_BT_LBS_SECURITY_ENABLED`
defaults to y and selects `BT_SMP`, which makes the SMP characteristic default
to `MCUMGR_TRANSPORT_BT_PERM_RW_AUTHEN`. So the central needs an authenticated
(passkey) bond before mcumgr works. The tag is DisplayOnly: it prints
`Passkey for <addr>: NNNNNN` to RTT, so reading it needs the J-Link anyway. A
phone that falls back to Just Works only gets an encrypted link, not an
authenticated one, so its SMP writes are refused.

Since the J-Link is needed either way, the simplest first flash is
`make flash`. After that the tag runs our MCUboot + app, where SMP is
`PERM_RW` (no pairing), and `make ota` works from then on.

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

- [ ] **1. Repo setup**: `west.yml`, Makefile, `.gitignore`, west workspace
      in the container.
- [ ] **2. Canary firmware**: BLE + SMP OTA + LED blink only. OTA it to the
      tag, confirm it boots, then OTA it again to prove updates still work.
- [ ] **3. Firmware v1**: BMI270 streaming over the protocol above, start/stop
      from the app or button, LED status.
- [ ] **4. Laptop recorder**: `host/` script (bleak). Logs to a file and plots
      live. Do one real skate session to look at the raw signal.
- [ ] **5. Android logger**: Kotlin foreground service. Connects, starts and
      stops recording, logs IMU + GPS, exports files. First test the stream
      with nRF Connect.
- [ ] **6. Analysis v1**: push detection and push rate, push/glide split,
      distance per push, fatigue drift over the session.
- [ ] **7. Power states**: ADXL367 wake-on-motion, System OFF, ready-mode
      advertising, then measure.
- [ ] **8. Second tag**: one per foot, clock sync, left/right metrics.
- [ ] **9. Technique**: orientation fusion (Madgwick), edge angle, push
      direction, crossovers.
- [ ] **Maybe**: live audio cues (push-rate beep), push detection on the tag,
      BME688 climb detection, phone-side OTA in our own app.

## Open questions

- The Zephyr BMI270 driver only has data-ready triggers, no FIFO. That works
  at 200 Hz, but a FIFO reader would let the CPU sleep longer. Decide once
  power is measured.
- Is 200 Hz enough, or does push onset need more? Check against the raw data
  from phase 4.
- Frame vs. cuff mount: compare both in an early session.
