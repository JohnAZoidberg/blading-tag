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
  them (and the last 8 packets a while longer), so packets in flight when
  the link drops are sent again.
- Advertises state, position and battery. Advertising is every 1 s in READY,
  and every ~100 ms while recording without a link, so the app reconnects
  quickly.
- Goes to System OFF on SLEEP, a 2 s button hold while idle, or after 10 min idle
  (`CONFIG_BLADING_IDLE_SLEEP_S`; off in test builds). ADXL367 motion or the
  button wakes it. See [Power states: status](#power-states-status).
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

A short press starts recording while idle and is ignored during recording.
To stop recording, hold for 2 s until the red flash, then release. The tag
stays awake; a connected recorder saves and ends the session. To sleep,
hold for 2 s and release while idle. Releasing early during recording does
nothing. See the [button table](../README.md#the-button).

| | |
|---|---|
| Green blip every 2 s | Recording, connected |
| Yellow blip every 1 s | Recording into the buffer, no link |
| Red blip every 1 s | Same, but the buffer is full: samples are being dropped |
| Blue blinking | IDENTIFY |
| Short press | Start recording while idle (green flash); ignored while recording |
| Hold 2 s (red flash), release while recording | Stop recording; stay awake |
| Hold 2 s (red flash), release while idle | System OFF |

## Power states: status

Tested on the tag (2026-11-07): streaming, STOP flush, link loss (rejoining a
running session, buffer overflow counted as dropped, no packets lost at a
drop, see the hardware tests), SET_POSITION, IDENTIFY, OTA from this firmware,
and sleep:

- **SLEEP and idle sleep reach System OFF and stay there.** Lying still,
  the tag goes to sleep 10 min after the last activity
  (`CONFIG_BLADING_IDLE_SLEEP_S`) and stops advertising. Moving it wakes it
  (boot log: `reset reason 0x00000100, last sleep reached System OFF`,
  `woken by motion`), and so does the button (`woken by button`).
- **Idle sleep only happens if motion can wake the tag.** If the ADXL367 INT1
  is still active after re-referencing (the tag moved, or the threshold is
  too low), the tag stays awake and tries again 10 min later. SLEEP and the
  long press go to sleep anyway, with only the button as a wake-up.
- **The GRTC must be stopped before System OFF.** Even with every compare
  channel disabled, a running GRTC woke the tag from System OFF within
  seconds (RESETREAS `0x800`). `sleep_work_fn()` stops it under PRIMASK, so
  MPSL's zero-latency IRQs can't re-arm it; Zephyr starts it again at boot.
- **Wake-up pins use GPIO SENSE, set with interrupts masked** right before
  `sys_poweroff()`. As Zephyr level interrupts, an active ADXL367 INT1 fired
  endlessly and the tag never got to System OFF.
- The boot log reads the GPIO LATCH register to say which pin woke the tag.

Still to do: measure the currents with a PPK2 (Ready, Asleep, Recording at
+7 dBm).

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

The button action rules, `proto.c`, and `samplebuf.c` are plain C, tested
with ztest on the `unit_testing` board (a host binary), including the example bytes from
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

OTA notes:

- `smpmgr upgrade` isn't used because it twice reported success without the
  image ending up pending. `host/ota.py` checks each step instead.
- The first BLE connect to the tag often fails; `host/ota.py` retries.
- The signed image must fit in slot1 (664 KiB minus the swap trailer). The
  tag rejects a bigger upload before writing anything. v1 is about 200 KB.
- BlueZ caches the GATT table across firmware changes. If new
  characteristics are missing after an update, run
  `bluetoothctl remove <address>` and reconnect.

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
