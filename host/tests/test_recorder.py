import asyncio
import json

import numpy as np
import pytest

from bladetag import protocol as p
from bladetag.faketag import FakeLink, FakeTag
from bladetag.link import ControlError
from bladetag.recorder import RecordOptions, Recorder
from bladetag.session import SessionWriter, load_session
from bladetag.timesync import fit_clocks


def run(coro):
    return asyncio.run(coro)


def make(tmp_path, speed=20.0, **kw):
    tag = FakeTag(speed=speed, loop_s=30.0, **kw)
    link = FakeLink(tag)
    w = SessionWriter(tmp_path / "s")
    return tag, link, w


def test_record_and_stop(tmp_path):
    tag, link, w = make(tmp_path)

    async def go():
        stop = asyncio.Event()
        rec = Recorder(link, w, RecordOptions(), log=lambda s: None)
        task = asyncio.create_task(rec.run(stop))
        await asyncio.sleep(0.5)  # ~10 s of tag time
        stop.set()
        return await task

    stats = run(go())
    assert stats.seq_gaps == 0 and stats.index_gaps == 0
    s = load_session(tmp_path / "s")
    t = s.tags[0]
    assert len(t.raw) > 1000
    assert t.gaps() == [] and t.seq_gaps() == 0
    assert t.statuses[0][1].state == p.State.READY            # read after connect
    assert t.statuses[-1][1].state == p.State.READY           # STOP marks the end
    assert any(st.state == p.State.RECORDING for _, st in t.statuses)
    assert s.meta["end_elapsed_ns"] is not None
    assert t.meta.session_id == 1 and t.meta.odr_hz == 200
    assert t.statuses[-1][1].samples_recorded == len(t.raw)
    fit = fit_clocks(t)
    assert fit.rate_hz == pytest.approx(200 * 1.004, rel=1e-3)


def test_link_loss_resumes_without_data_loss(tmp_path):
    tag, link, w = make(tmp_path)

    async def go():
        stop = asyncio.Event()
        rec = Recorder(link, w, RecordOptions(), log=lambda s: None)
        task = asyncio.create_task(rec.run(stop))
        await asyncio.sleep(0.3)
        link.drop()
        link.refuse_connect = True
        await asyncio.sleep(0.3)   # a few seconds of tag time buffered
        link.refuse_connect = False
        await asyncio.sleep(0.5)
        stop.set()
        return await task

    stats = run(go())
    assert stats.reconnects == 1
    t = load_session(tmp_path / "s").tags[0]
    assert t.gaps() == [] and t.seq_gaps() == 0
    assert np.array_equal(t.sample_index, np.arange(len(t.raw)))
    assert t.meta.session_id == 1  # did not START again


def test_resume_tag_that_is_already_recording(tmp_path):
    tag, link, w = make(tmp_path)

    async def go():
        # First client starts the tag and vanishes.
        first = FakeLink(tag)
        await first.connect()
        await first.subscribe(lambda b: None, lambda b: got.append(p.DataPacket.parse(b).n))
        await first.write_control(p.cmd_start())
        await asyncio.sleep(0.05)
        first.drop()
        await asyncio.sleep(0.1)
        stop = asyncio.Event()
        rec = Recorder(link, w, RecordOptions(duration_s=0.2), log=lambda s: None)
        return await rec.run(stop)

    got = []
    run(go())
    t = load_session(tmp_path / "s").tags[0]
    assert t.meta.session_id == 1
    # Picks up exactly where the first client left off: the samples buffered
    # while nobody was connected arrive first.
    assert t.sample_index[0] == sum(got)
    assert t.gaps() == []


def test_control_errors(tmp_path):
    tag, link, w = make(tmp_path)

    async def go():
        await link.connect()
        with pytest.raises(ControlError) as e:
            await link.write_control(p.cmd_start())  # Data notifications not enabled
        assert e.value.code == p.ErrorCode.BAD_STATE
        await link.subscribe(lambda b: None, lambda b: None)
        with pytest.raises(ControlError) as e:
            await link.write_control(p.cmd_start(123, 16, 2000))
        assert e.value.code == p.ErrorCode.BAD_PARAM
        with pytest.raises(ControlError) as e:
            await link.write_control(b"\x42")
        assert e.value.code == p.ErrorCode.UNKNOWN_OPCODE
        await link.write_control(p.cmd_set_position(p.Position.RIGHT))
        await link.write_control(p.cmd_start())
        with pytest.raises(ControlError) as e:
            await link.write_control(p.cmd_start())
        assert e.value.code == p.ErrorCode.BAD_STATE
        await link.write_control(p.cmd_stop())
        await link.disconnect()

    run(go())
    assert tag.position == p.Position.RIGHT


def test_tag_stopping_on_its_own_ends_the_recording(tmp_path):
    tag, link, w = make(tmp_path)

    async def go():
        stop = asyncio.Event()
        rec = Recorder(link, w, RecordOptions(), log=lambda s: None)
        task = asyncio.create_task(rec.run(stop))
        await asyncio.sleep(0.3)
        tag._stop(flush=True)  # e.g. the button
        return await asyncio.wait_for(task, 2.0)

    run(go())
    t = load_session(tmp_path / "s").tags[0]
    assert t.statuses[-1][1].state == p.State.READY
    assert t.gaps() == [] and t.statuses[-1][1].samples_recorded == len(t.raw)


def test_stop_while_reconnecting(tmp_path):
    tag, link, w = make(tmp_path)

    async def go():
        stop = asyncio.Event()
        rec = Recorder(link, w, RecordOptions(), log=lambda s: None)
        task = asyncio.create_task(rec.run(stop))
        await asyncio.sleep(0.3)
        link.refuse_connect = True
        link.drop()
        await asyncio.sleep(0.2)
        stop.set()
        return await asyncio.wait_for(task, 2.0)

    run(go())
    assert len(load_session(tmp_path / "s").tags[0].raw) > 500


def test_duration_ends_reconnecting(tmp_path):
    tag, link, w = make(tmp_path)

    async def go():
        rec = Recorder(link, w, RecordOptions(duration_s=0.5), log=lambda s: None)
        task = asyncio.create_task(rec.run(asyncio.Event()))
        await asyncio.sleep(0.2)
        link.refuse_connect = True
        link.drop()
        return await asyncio.wait_for(task, 2.0)

    run(go())


def test_session_json_has_the_session_while_recording(tmp_path):
    tag, link, w = make(tmp_path)
    tag.session_id = 41

    async def go():
        stop = asyncio.Event()
        rec = Recorder(link, w, RecordOptions(), log=lambda s: None)
        task = asyncio.create_task(rec.run(stop))
        await asyncio.sleep(0.2)
        meta = json.loads((tmp_path / "s" / "session.json").read_text())
        stop.set()
        await task
        return meta

    meta = run(go())
    assert meta["tags"][0]["session_id"] == 42  # not 41, the one before START
