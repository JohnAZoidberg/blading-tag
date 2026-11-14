"""Hardware tests against a real tag running the test build.

The J-Link plays the button (firmware/src/testhook.c), the laptop plays the
phone. See README.md here for the setup.
"""
import asyncio
import os
import time

import numpy as np
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
        assert (await status(ready)).state == p.State.RECORDING
        await asyncio.to_thread(hooks.button_stop_hold)
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


def test_phone_started_recording_requires_button_hold(ready, hooks):
    async def go():
        async with open_link(ready) as link:
            await link.write_control(p.cmd_start())
            await asyncio.to_thread(hooks.button_short)
            assert p.Status.parse(await link.read_status()).state == p.State.RECORDING
            await asyncio.to_thread(hooks.button_stop_hold)
            for _ in range(50):
                if p.Status.parse(await link.read_status()).state == p.State.READY:
                    break
                await asyncio.sleep(0.1)
            assert p.Status.parse(await link.read_status()).state == p.State.READY

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


# More with BLADETAG_DROPS=20: each drop catches a packet in flight only sometimes.
DROPS = int(os.environ.get("BLADETAG_DROPS", "5"))


def test_link_drops_lose_no_samples(ready, tmp_path):
    """Drop the link a few times while data flows, the way a weak link does.
    Packets in flight at each drop never got acknowledged, so the tag sends
    them again after the reconnect: the session must have no gaps at all."""
    from bladetag.link import BleakLink
    from bladetag.recorder import Recorder
    from bladetag.session import SessionWriter, load_session

    async def go():
        writer = SessionWriter(tmp_path / "s")
        rec = Recorder(BleakLink(ready), writer)
        stop = asyncio.Event()

        async def dropper():
            drops = 0
            while drops < DROPS:
                # Wait for a link with data flowing for a few seconds.
                n0 = rec.stats.reconnects
                await asyncio.sleep(4)
                if not rec.link.is_connected or rec.stats.reconnects != n0:
                    continue
                proc = await asyncio.create_subprocess_exec(
                    "bluetoothctl", "disconnect", ready,
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
                await proc.wait()
                drops += 1
                while rec.stats.reconnects < n0 + 1:
                    await asyncio.sleep(0.2)
            await asyncio.sleep(4)
            stop.set()

        d = asyncio.create_task(dropper())
        try:
            stats = await asyncio.wait_for(rec.run(stop), DROPS * 60)
        finally:
            d.cancel()
            writer.close()
        return stats

    stats = run(go())
    assert stats.reconnects >= DROPS
    t = load_session(tmp_path / "s").tags[0]
    last = t.statuses[-1][1]
    assert last.state == p.State.READY
    assert last.samples_dropped == 0
    assert t.gaps() == [], f"gaps {t.gaps()}"
    assert t.seq_gaps() == 0
    assert len(t.sample_index) == last.samples_recorded
    d = np.diff(t.pkt_seq) % 65536
    resent = t.duplicate_packets + int(np.sum((d == 0) | (d >= 32768)))
    print(f"{stats.reconnects} reconnects, {last.samples_recorded} samples, "
          f"{resent} resent packet(s) seen")


def test_link_timeouts_lose_no_samples(ready, tmp_path):
    """Like a phone going out of range: halt the tag's CPU (and with it the
    radio) for longer than the supervision timeout, so both sides lose the
    link without either host tearing it down. Nothing may get lost on the
    link: no seq gaps, and every missing sample is one the tag counted as
    dropped (its IMU FIFO overflows while halted)."""
    from bladetag.jlink import JLink
    from bladetag.link import BleakLink
    from bladetag.recorder import Recorder
    from bladetag.session import SessionWriter, load_session

    serial = os.environ.get("BLADETAG_JLINK")
    if not serial:
        pytest.skip("set BLADETAG_JLINK=<J-Link serial> (the tag must sit in the DK's DEBUG OUT)")
    jlink = JLink(serial)

    async def go():
        writer = SessionWriter(tmp_path / "s")
        rec = Recorder(BleakLink(ready), writer)
        stop = asyncio.Event()

        async def dropper():
            drops = 0
            while drops < DROPS:
                n0 = rec.stats.reconnects
                await asyncio.sleep(3)
                if not rec.link.is_connected or rec.stats.reconnects != n0:
                    continue
                # Supervision timeout is 4 s.
                await asyncio.to_thread(jlink.commander, ["halt", "Sleep 6000", "go"])
                drops += 1
                while rec.stats.reconnects < n0 + 1:
                    await asyncio.sleep(0.2)
            await asyncio.sleep(3)
            stop.set()

        d = asyncio.create_task(dropper())
        try:
            stats = await asyncio.wait_for(rec.run(stop), DROPS * 60)
        finally:
            d.cancel()
            writer.close()
        return stats

    stats = run(go())
    assert stats.reconnects >= DROPS
    t = load_session(tmp_path / "s").tags[0]
    last = t.statuses[-1][1]
    assert last.state == p.State.READY
    assert t.seq_gaps() == 0, f"{t.seq_gaps()} packet(s) lost on the link"
    missing = sum(n for _, n in t.gaps())
    assert missing == last.samples_dropped, f"{missing} missing, tag dropped {last.samples_dropped}"
    assert len(t.sample_index) + missing == last.samples_recorded
    print(f"{stats.reconnects} reconnects, {last.samples_recorded} samples, "
          f"{last.samples_dropped} dropped by the tag while halted")
