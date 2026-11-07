# firmware

Zephyr / nRF Connect SDK v3.4.0 app for the nRF54L15 Tag
(board target `nrf54l15tag/nrf54l15/cpuapp`).

Version 0.2.0 is firmware v1 (phase 3 in [../PLAN.md](../PLAN.md)). It
implements the BLE protocol in [../PROTOCOL.md](../PROTOCOL.md) §1 and has
been tested on a tag with the host recorder: 30 s at 200 Hz, 0 samples dropped.

What it does:

- Streams BMI270 accelerometer + gyroscope data at 200 Hz over BLE while the
  app asks for it (START/STOP), or after a button press.
- Keeps recording into a buffer (8192 samples, ~41 s at 200 Hz) while the
  link is down, and stops on its own after 5 min without a link.
- Advertises state, position and battery. Advertising is every 1 s in READY,
  and every ~100 ms while recording without a link, so the app reconnects
  quickly.
- Goes to System OFF on SLEEP, a 2 s button hold, or after 10 min in READY
  with no link and no motion. ADXL367 motion or the button wakes it.
- Accepts firmware updates over BLE (MCUboot + mcumgr/SMP), and confirms its
  image once Bluetooth is up.

Wake-up from System OFF and the power draw have not been checked on a tag yet
(phase 7).

Build, OTA and flash run from the repo root via the `Makefile`.

## Code

| File | What it does |
|---|---|
| `src/main.c` | Startup, image confirm |
| `src/app.c` | State machine: START/STOP, link loss, Status, button, idle sleep |
| `src/ble.c` | Advertising and the Blading GATT service (SMP comes from `prj.conf`) |
| `src/proto.c` | Encodes Info/Status/Data/advertising, parses Control writes |
| `src/imu.c` | BMI270: driver init, then the FIFO with a ~100 ms watermark interrupt |
| `src/samplebuf.c` | Ring buffer between the IMU and Data notifications |
| `src/motion.c` | ADXL367 wake-on-motion |
| `src/battery.c` | Coin-cell voltage from the SAADC VDD input |
| `src/led.c` | LED patterns |
| `Kconfig` | Buffer size, link-loss and idle timeouts, motion threshold, low battery |

The IMU interrupt edge is timestamped in the ISR, which dates each FIFO batch
without waking up for every sample. A FIFO overflow is counted as dropped
samples, so `sample_index` never slips silently.

`CONFIG_RESET_ON_FATAL_ERROR` is on: a crash in a test image resets the tag,
and MCUboot reverts to the previous image.

### LED and button

| | |
|---|---|
| Green blip every 2 s | Recording, connected |
| Yellow blip every 1 s | Recording into the buffer, no link |
| Blue blinking | IDENTIFY |
| Short press | Start recording (green flash) or stop (red flash) |
| Hold 2 s (red flash), release | System OFF |

## Unit tests

`proto.c` and `samplebuf.c` are plain C, tested with ztest on the
`unit_testing` board (a host binary), including the example bytes from
PROTOCOL.md. Inside the toolchain container (see `CONTAINER_RUN` in the
Makefile):

```sh
west build -b unit_testing --no-sysbuild firmware/tests/unit -d firmware/build-unit
firmware/build-unit/testbinary
```

## Building and updating

From the repo root (only podman or docker and [uv](https://docs.astral.sh/uv/)
are needed):

```sh
make workspace                    # once: west init + update in the container
make build                        # MCUboot + app -> firmware/build/
make ota TAG=C0:BB:4A:A7:0D:57    # update the tag over BLE
```

`make ota` runs [`host/ota.py`](../host/ota.py). It uploads the signed app to
slot 1, marks it for a test boot, resets the tag, and then checks that the new
image is running and has confirmed itself. If the app doesn't call
`boot_write_img_confirmed()`, the next reset reverts it.

This works on a tag straight out of the box. The factory firmware
(`Nordic_LBS`) already has SMP, no pairing needed, and its MCUboot uses the
default dev key, which we sign with too. Phones can update with nRF Connect
Device Manager and `firmware/build/dfu_application.zip`.

### Flash layout

The factory MCUboot uses NCS's FLPR partition variant, not the NCS 3.4
default. `boards/nrf54l15tag_nrf54l15_cpuapp.overlay` sets it for the app,
and `sysbuild/mcuboot.overlay` sets it for our MCUboot:

| Partition | Address | Size |
|---|---|---|
| mcuboot | 0x000000 | 62 KiB |
| slot0 | 0x010000 | 664 KiB |
| slot1 | 0x0b6000 | 664 KiB |
| storage (settings) | 0x15c000 | 36 KiB |

Don't change it: tags updated over BLE keep the factory MCUboot. If the app's
slot0 doesn't match, the image boots once and then reverts.

The J-Link (`make flash`) is only for recovering a tag that won't boot.
