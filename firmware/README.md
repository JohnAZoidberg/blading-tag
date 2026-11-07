# firmware

Zephyr / nRF Connect SDK v3.4.0 app for the nRF54L15 Tag
(board target `nrf54l15tag/nrf54l15/cpuapp`).

Version 0.2.0 is firmware v1 (phase 3 in [../PLAN.md](../PLAN.md)). It
implements the BLE protocol in [../PROTOCOL.md](../PROTOCOL.md) §1 and has
been tested on a tag with the host recorder: 30 s at 200 Hz, 0 samples dropped.

What it does:

- Streams BMI270 accelerometer + gyroscope data at 200 Hz over BLE while the
  app asks for it (START/STOP), or after a button press.
- Keeps recording into a buffer (14336 samples, ~72 s at 200 Hz) while the
  link is down or too slow, and stops on its own after 5 min without a link.
  Samples stay in the buffer until the phone's link layer has acknowledged
  them, so packets in flight when the link drops are sent again.
- Advertises state, position and battery. Advertising is every 1 s in READY,
  and every ~100 ms while recording without a link, so the app reconnects
  quickly.
- Goes to System OFF on SLEEP or a 2 s button hold. ADXL367 motion or the
  button is meant to wake it. **This is still being debugged**, see
  [Power states: status](#power-states-status). The 10 min idle sleep is off
  (`CONFIG_BLADING_IDLE_SLEEP_S=0`) until it works.
- Accepts firmware updates over BLE (MCUboot + mcumgr/SMP), and confirms its
  image once Bluetooth is up.

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

## Power states: status

Tested on the tag (2026-11-07): streaming, STOP flush, link loss (rejoining a
running session, buffer overflow counted as dropped), SET_POSITION, IDENTIFY,
OTA from this firmware (0.1.0 -> 0.2.0). Not working yet: **SLEEP / System
OFF**. What we know:

1. **Fixed: interrupt storm.** The wake-up pins used to be armed as Zephyr
   level interrupts before power-off. The ADXL367 INT1 (P0.03) was high, so
   the level interrupt fired endlessly and the tag livelocked before reaching
   System OFF (seen with gdb). Now SENSE is set directly on the pins with
   interrupts locked, right before `sys_poweroff()`, and the ADXL367 is only
   used as a wake source if INT1 is low after re-arming.
2. **Open: the GRTC wakes the tag from System OFF** within seconds
   (RESETREAS = `0x800`, GRTC). A retained marker (`GPREGRET[1]`, logged at
   boot as "last sleep reached System OFF") confirms it really was off. At
   `z_sys_poweroff()` a debugger showed every GRTC `CC[n].CCEN` = 0 and no
   compare events pending, so it isn't an armed compare. The current code also
   stops the GRTC (`TASKS_STOP`) before power-off, with interrupts masked via
   PRIMASK so MPSL's zero-latency IRQs can't re-arm it. That build is flashed
   but not cleanly tested yet. One SLEEP did stay asleep until a GPIO wake
   (RESETREAS `0x140`), but it's unclear which build was running.
3. **Open: why ADXL367 INT1 is high** on a tag lying still. It may be the
   referenced activity setup in wake-up mode (`motion.c`), or the threshold.
   `motion_prepare_sysoff()` now logs STATUS, the INT1 level and the
   interrupt count.

Next steps:

- Run one clean SLEEP test (no debugger attached): SLEEP, wait 2 min, connect.
  Then read the boot log line `reset reason ..., last sleep ...`.
- If the GRTC still wakes it: compare with Nordic's
  `nrf/samples/bluetooth/peripheral_power_profiling` on this board, and check
  the nRF54L15 errata and GRTC docs about System OFF.
- Fix INT1, then test wake-on-motion. Then set `CONFIG_BLADING_IDLE_SLEEP_S`
  back to 600 and measure the current with a PPK2.

Debugging notes:

- The app's RTT control block is `_SEGGER_RTT` (see `nm zephyr.elf`).
  MCUboot's log fills much of the buffer before the app starts. Nothing reads
  it unless a host is attached, and when it's full new lines are dropped. The
  most reliable read is a memory dump of `_acUpBuffer` with `JLinkExe
  savebin`.
- gdb in the toolchain container works against `JLinkGDBServer` on the host
  (`podman run --network host ...`). Without `CONFIG_DEBUG_THREAD_INFO` there
  is no thread awareness; run `maintenance flush register-cache` after
  `monitor halt`.
- A tag that is confirmed but doesn't advertise can't be updated over BLE.
  Flash the app over J-Link instead:
  `nrfutil device program --firmware .../zephyr.signed.hex --options
  chip_erase_mode=ERASE_RANGES_TOUCHED_BY_FIRMWARE`.
  This keeps MCUboot and settings.

## Unit tests

`proto.c` and `samplebuf.c` are plain C, tested with ztest on the
`unit_testing` board (a host binary), including the example bytes from
PROTOCOL.md. Inside the toolchain container (see `CONTAINER_RUN` in the
Makefile):

```sh
west build -b unit_testing --no-sysbuild firmware/tests/unit -d firmware/build-unit
firmware/build-unit/testbinary
```

## Hardware tests

`make build-test ota-test test-hw` runs pytest tests against a real tag in
the DK's DEBUG OUT header. `test.conf` adds `src/testhook.c`, a RAM mailbox
the J-Link writes commands into, such as "short button press". See
[../host/tests/hw/README.md](../host/tests/hw/README.md).

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
