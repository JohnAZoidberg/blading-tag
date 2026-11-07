# firmware

Zephyr / nRF Connect SDK v3.4.0 app for the nRF54L15 Tag
(board target `nrf54l15tag/nrf54l15/cpuapp`).

Phase 2 (canary: BLE + OTA + LED) is done. Phases 3 and 7 are in [../PLAN.md](../PLAN.md).

What it will do:

- Stream BMI270 accelerometer + gyroscope data at 200 Hz over BLE when the
  app asks for it.
- Sleep when idle, and wake on motion via the ADXL367.
- Accept firmware updates over BLE (MCUboot + mcumgr/SMP).

Build, OTA and flash run from the repo root via the `Makefile`.

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
