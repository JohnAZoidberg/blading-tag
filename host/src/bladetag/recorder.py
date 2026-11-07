"""Record one tag into a session folder (PROTOCOL.md 1.2, 1.5, 1.9, 2.3)."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Callable

from . import protocol as p
from .link import ControlError, TagLink
from .session import RecordKind, SessionWriter, TagLog

LINK_LOSS_GIVE_UP_S = 300.0 + 30.0  # tag gives up after 5 min; allow some slack


@dataclass
class RecordOptions:
    odr_hz: int = 0           # 0 = tag default
    accel_range_g: int = 0
    gyro_range_dps: int = 0
    duration_s: float | None = None
    stop_timeout_s: float = 10.0


@dataclass
class Stats:
    packets: int = 0
    samples: int = 0
    seq_gaps: int = 0
    index_gaps: int = 0
    reconnects: int = 0
    last_status: p.Status | None = None
    started_mono: float = field(default_factory=time.monotonic)
    _next_seq: int | None = None
    _next_index: int | None = None

    def on_packet(self, pkt: p.DataPacket) -> None:
        self.packets += 1
        self.samples += pkt.n
        if self._next_seq is not None and pkt.seq != self._next_seq:
            self.seq_gaps += (pkt.seq - self._next_seq) % 65536
        if self._next_index is not None and pkt.sample_index != self._next_index:
            self.index_gaps += 1
        self._next_seq = (pkt.seq + 1) % 65536
        self._next_index = pkt.sample_index + pkt.n

    def line(self) -> str:
        s = self.last_status
        el = time.monotonic() - self.started_mono
        parts = [f"{el:7.1f}s", f"{self.samples} samples", f"{self.samples / max(el, 1e-3):6.1f}/s"]
        if s:
            parts += [s.state.name, f"{s.battery_mv} mV", f"buf {s.buffer_fill}"]
            if s.samples_dropped:
                parts.append(f"DROPPED {s.samples_dropped}")
            parts += s.flag_names()
        if self.seq_gaps or self.index_gaps:
            parts.append(f"gaps seq={self.seq_gaps} idx={self.index_gaps}")
        if self.reconnects:
            parts.append(f"reconnects {self.reconnects}")
        return " | ".join(parts)


class RecordError(Exception):
    pass


class Recorder:
    def __init__(self, link: TagLink, writer: SessionWriter, opts: RecordOptions | None = None,
                 on_packet: Callable[[p.DataPacket, p.Status | None], None] | None = None,
                 log: Callable[[str], None] = print):
        self.link = link
        self.writer = writer
        self.opts = opts or RecordOptions()
        self.on_packet = on_packet
        self.log = log
        self.stats = Stats()
        self._taglog: TagLog | None = None
        self._lost = asyncio.Event()
        self._ready = asyncio.Event()
        self._stopping = False
        link.on_disconnect = self._on_disconnect

    # -- callbacks (run on the event loop)
    def _on_disconnect(self) -> None:
        self._lost.set()

    def _on_status(self, payload: bytes) -> None:
        self._taglog.write(RecordKind.STATUS, payload)
        try:
            s = p.Status.parse(payload)
        except p.ProtocolError:
            return
        self.stats.last_status = s
        if s.state == p.State.READY:
            self._ready.set()
        else:
            self._ready.clear()

    def _on_data(self, payload: bytes) -> None:
        self._taglog.write(RecordKind.DATA, payload)
        try:
            pkt = p.DataPacket.parse(payload)
        except p.ProtocolError:
            return
        if pkt.samples is None:
            return
        self.stats.on_packet(pkt)
        if self.on_packet:
            self.on_packet(pkt, self.stats.last_status)

    # -- steps
    async def _setup(self) -> p.Status:
        """Subscribe, then read Status (logged as a record)."""
        if self.link.mtu < p.MIN_MTU:
            self.log(f"warning: MTU is {self.link.mtu}; the tag needs >= {p.MIN_MTU}")
        await self.link.subscribe(self._on_status, self._on_data)
        raw = await self.link.read_status()
        self._on_status(raw)
        status = p.Status.parse(raw)
        self.writer.tags[self.link.address].update_from_status(status)
        self.writer.save()
        return status

    async def _reconnect(self) -> p.Status | None:
        self.log("link lost, reconnecting ...")
        deadline = time.monotonic() + LINK_LOSS_GIVE_UP_S
        delay = 0.5
        while time.monotonic() < deadline and not self._stopping:
            try:
                await self.link.connect()
                self._lost.clear()
                st = await self._setup()
                self.stats.reconnects += 1
                self.log(f"reconnected: {st.state.name}, {st.buffer_fill} samples buffered")
                return st
            except Exception as e:  # noqa: BLE001 - any failure: retry
                self.log(f"  reconnect failed: {e}")
                await asyncio.sleep(delay)
                delay = min(delay * 2, 5.0)
        return None

    async def run(self, stop: asyncio.Event) -> Stats:
        await self.link.connect()
        info_raw = await self.link.read_info()
        info = p.Info.parse(info_raw)
        if info.protocol_version != p.PROTOCOL_VERSION:
            await self.link.disconnect()
            raise RecordError(f"tag speaks protocol v{info.protocol_version}, we support v{p.PROTOCOL_VERSION};"
                              " update the firmware or this tool")
        self.log(f"connected to {self.link.address}: fw {info.fw_string}, MTU {self.link.mtu}")
        self._taglog = self.writer.add_tag(self.link.address, info_raw)
        status = await self._setup()

        if status.state == p.State.RECORDING:
            self.log(f"tag is already recording (session {status.session_id}); continuing it")
        else:
            try:
                await self.link.write_control(
                    p.cmd_start(self.opts.odr_hz, self.opts.accel_range_g, self.opts.gyro_range_dps))
            except ControlError:
                await self.link.disconnect()
                raise
            self._ready.clear()
            self.log("recording started")

        deadline = time.monotonic() + self.opts.duration_s if self.opts.duration_s else None
        while True:
            waits = [asyncio.ensure_future(stop.wait()), asyncio.ensure_future(self._lost.wait())]
            timeout = None if deadline is None else max(0.0, deadline - time.monotonic())
            done, pending = await asyncio.wait(waits, timeout=min(timeout or 1.0, 1.0),
                                               return_when=asyncio.FIRST_COMPLETED)
            for w in pending:
                w.cancel()
            self._taglog.flush()
            if stop.is_set() or (deadline is not None and time.monotonic() >= deadline):
                break
            if self._lost.is_set():
                st = await self._reconnect()
                if st is None:
                    self.log("could not reconnect; giving up")
                    break
                if st.state != p.State.RECORDING:
                    self.log("tag stopped recording on its own (link lost too long); session ends here")
                    break

        await self._finish()
        return self.stats

    async def _finish(self) -> None:
        self._stopping = True
        if self.link.is_connected:
            try:
                await self.link.write_control(p.cmd_stop())
                await asyncio.wait_for(self._ready.wait(), self.opts.stop_timeout_s)
                self.log("recording stopped, all buffered samples received")
            except (ControlError, asyncio.TimeoutError, ConnectionError) as e:
                self.log(f"warning: clean stop failed: {e!r}")
            await self.link.disconnect()
        if self.stats.last_status:
            self.writer.tags[self.link.address].update_from_status(self.stats.last_status)
        self.writer.close()
