# Hardware tests

pytest tests against a real tag. The laptop plays the phone over BLE, and a
J-Link plays the button: test builds of the firmware have a small RAM mailbox
(`firmware/src/testhook.c`, turned on by `firmware/test.conf`). The J-Link
writes a command into it while the CPU keeps running, and the firmware runs
the same code as a real button press.

Without `BLADETAG_HW` these tests are skipped, so a plain `pytest` stays
offline.

## Setup

1. Put the tag in the DK's **DEBUG OUT** header. The DK's J-Link reaches the
   tag only there; otherwise it talks to the DK's own chip. Check with
   `nrfutil device device-info --serial-number <J-Link serial>` (deviceName
   must be nRF54L15). Keep the coin cell in: the header doesn't power the tag.
2. Build and install the test firmware (from the repo root):

   ```sh
   make build-test   # firmware/build-test, doesn't touch firmware/build
   make ota-test     # over BLE, like make ota
   ```

3. Run:

   ```sh
   make test-hw      # TAG=... JLINK=... to override the defaults
   ```

   or `BLADETAG_HW=<addr> BLADETAG_JLINK=<serial> pytest tests/hw -v` in
   `host/`. `BLADETAG_ELF` points at a different `zephyr.elf` (default:
   `firmware/build-test/firmware/zephyr/zephyr.elf`).

The tests check that the tag runs the build that ELF came from (a magic word
in the mailbox) before they write anything. A failed test gets the tag's
recent RTT log attached to its report.

Put the normal firmware back afterwards (`make build ota`): the test build
polls the mailbox every 20 ms, which costs power.

## What's covered

| Test | Checks |
|---|---|
| `test_button_start_keeps_advertising` | Button start with no phone: adverts say RECORDING within 3 s and keep coming (no gap > 3 s for 20 s) |
| `test_button_stop_advertises_ready` | Short press is ignored while recording; hold-stop release advertises READY again |
| `test_phone_started_recording_requires_button_hold` | Phone-started recording ignores a short press and stops on hold release |
| `test_ble_start_stop` | START from the laptop, Data flows, STOP ends with a READY Status |
| `test_stop_from_another_connection` | The app's Stop button: STOP without subscribing ends a recording someone else started |
| `test_sleep_refused_while_recording` | SLEEP gets BAD_STATE while recording |
| `test_link_drops_lose_no_samples` | The recorder survives `bluetoothctl disconnect` mid-recording (`BLADETAG_DROPS`, default 5) with no samples lost: packets in flight are resent |
| `test_link_timeouts_lose_no_samples` | Same with real supervision timeouts: the J-Link halts the tag for 6 s. Needs the J-Link but not the test build |

Not covered here:

- **Sleep and wake**: with a debugger attached, System OFF is only emulated,
  so it says nothing about the real thing. Test that with the tag out of the
  header.
- **The 5-minute link-loss stop**: too slow for every run.

## Things learned the hard way

- A BlueZ scan session that's already running keeps reporting a tag's
  adverts but can keep the old manufacturer data: it said RECORDING for 17 s
  while the tag (its RAM, and a fresh scan) said READY. The tests start a new
  scan session after each state change.
- The laptop misses many of the 1 s READY adverts (6 of 15 in 15 s), so the
  READY gap limit is loose (8 s). It only catches a tag that goes silent.
