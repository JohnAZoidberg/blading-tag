"""Hardware tests against a real tag running the test build.

The J-Link plays the button (firmware/src/testhook.c), the laptop plays the
phone. See README.md here for the setup.
"""
import asyncio
import time

import pytest

from bladetag import protocol as p
from bladetag.link import ControlError, open_link

from hwutil import status, watch_adverts

# Fast advertising while recording is every 100-150 ms. Allow for a busy
# radio on the laptop side, but not for the tag going quiet.
MAX_GAP_S = 3.0
WATCH_S = 20.0
# Slow advertising (READY) is every ~1 s, but a laptop misses many of those
# (seen: 6 of 15 in 15 s, a 5 s hole). Only catch a tag that goes silent.
MAX_GAP_READY_S = 8.0
WATCH_READY_S = 10.0


def run(coro):
    return asyncio.run(coro)


def test_button_start_keeps_advertising(ready, hooks):
    """A button start without a phone: the tag must keep advertising, now as
    RECORDING, so the app can find it and pick up the recording."""
    async def go():
        await asyncio.to_thread(hooks.button_short)
        # A scan session per action: see watch_adverts.
        async with watch_adverts(ready) as a:
            t_rec = await a.wait_state(p.State.RECORDING, 3)
            await asyncio.sleep(WATCH_S)
            n, gap, states = a.stats(t_rec, time.monotonic())
        assert states == {p.State.RECORDING}, f"advertised {states} while recording"
        assert gap < MAX_GAP_S, f"{n} adverts in {WATCH_S:.0f} s, longest gap {gap:.1f} s"
        st = await status(ready)
        assert st.state == p.State.RECORDING
        assert st.samples_recorded > 0

    run(go())


def test_button_stop_advertises_ready(ready, hooks):
    async def go():
        await asyncio.to_thread(hooks.button_short)
        async with watch_adverts(ready) as a:
            await a.wait_state(p.State.RECORDING, 3)
        await asyncio.sleep(2)
        await asyncio.to_thread(hooks.button_short)
        async with watch_adverts(ready) as a:
            t_ready = await a.wait_state(p.State.READY, 5)
            await asyncio.sleep(WATCH_READY_S)
            n, gap, states = a.stats(t_ready, time.monotonic())
        assert states == {p.State.READY}
        assert gap < MAX_GAP_READY_S, f"{n} adverts in {WATCH_READY_S:.0f} s, longest gap {gap:.1f} s"

    run(go())


def test_ble_start_stop(ready):
    """What the app does: START with notifications on, data flows, STOP
    flushes and ends in a READY Status."""
    async def go():
        statuses: list[p.Status] = []
        data: list[bytes] = []
        async with open_link(ready) as link:
            await link.subscribe(lambda b: statuses.append(p.Status.parse(b)), data.append)
            await link.write_control(p.cmd_start())
            await asyncio.sleep(3)
            assert data, "no Data packets after START"
            assert p.Status.parse(await link.read_status()).state == p.State.RECORDING
            await link.write_control(p.cmd_stop())
            for _ in range(50):
                if statuses and statuses[-1].state == p.State.READY:
                    break
                await asyncio.sleep(0.1)
            assert statuses and statuses[-1].state == p.State.READY, "no READY Status after STOP"

    run(go())


def test_stop_from_another_connection(ready, hooks):
    """The app's Stop button: connect to a tag recording for someone else,
    write STOP without subscribing, disconnect. The tag must end up READY."""
    async def go():
        await asyncio.to_thread(hooks.button_short)
        assert (await status(ready)).state == p.State.RECORDING
        async with open_link(ready) as link:
            await link.write_control(p.cmd_stop())
        async with watch_adverts(ready) as a:
            await a.wait_state(p.State.READY, 5, since=0)
        assert (await status(ready)).state == p.State.READY

    run(go())


def test_sleep_refused_while_recording(ready, hooks):
    """SLEEP is only allowed in READY (the app greys the button out)."""
    async def go():
        await asyncio.to_thread(hooks.button_short)
        async with open_link(ready) as link:
            with pytest.raises(ControlError) as e:
                await link.write_control(p.cmd_sleep())
            assert e.value.code == p.ErrorCode.BAD_STATE
            assert p.Status.parse(await link.read_status()).state == p.State.RECORDING

    run(go())
