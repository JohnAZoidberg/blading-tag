# firmware

Zephyr / nRF Connect SDK v3.4.0 app for the nRF54L15 Tag
(board target `nrf54l15tag/nrf54l15/cpuapp`).

**Not written yet.** Phases 2–3 and 7 in [../PLAN.md](../PLAN.md).

What it will do:

- Stream BMI270 accelerometer + gyroscope data at 200 Hz over BLE when the
  app asks for it.
- Sleep when idle, and wake on motion via the ADXL367.
- Accept firmware updates over BLE (MCUboot + mcumgr/SMP).

Build, OTA and flash run from the repo root via the `Makefile`.
