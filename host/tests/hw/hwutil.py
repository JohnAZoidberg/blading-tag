"""Helpers for the hardware tests: advert watching and getting the tag READY."""
from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager

from bladetag import protocol as p


class Adverts:
    """Every advert from one tag, with BlueZ's duplicate filter off, so a
    tag that stops advertising shows up as a gap."""

    def __init__(self, addr: str):
        self.addr = addr.upper()
        self.seen: list[tuple[float, p.State]] = []

    def _cb(self, dev, adv):
        if dev.address.upper() != self.addr:
            return
        mfr = adv.manufacturer_data.get(p.COMPANY_ID)
        if mfr:
            self.seen.append((time.monotonic(), p.Advert.parse(mfr).state))

    async def wait_state(self, state: p.State, timeout: float, since: float | None = None) -> float:
        """Time of the first advert with ``state`` after ``since``."""
        since = time.monotonic() if since is None else since
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for t, s in self.seen:
                if t >= since and s == state:
                    return t
            await asyncio.sleep(0.05)
        last = [s.name for t, s in self.seen if t >= since][-5:]
        raise AssertionError(f"no {state.name} advert within {timeout} s (last seen: {last or 'none'})")

    def stats(self, start: float, end: float) -> tuple[int, float, set[p.State]]:
        """Adverts in [start, end]: count, longest gap (s), states."""
        ts = [start] + [t for t, _ in self.seen if start <= t <= end] + [end]
        gap = max(b - a for a, b in zip(ts, ts[1:]))
        return len(ts) - 2, gap, {s for t, s in self.seen if start <= t <= end}


@asynccontextmanager
async def watch_adverts(addr: str):
    """Collect adverts while the block runs. Start a new one after each
    state change: a BlueZ scan session that's already running keeps
    reporting adverts but can keep the old manufacturer data (seen: the tag
    advertised READY for 17 s while a running session still said
    RECORDING, and a fresh one said READY)."""
    from bleak import BleakScanner

    a = Adverts(addr)
    async with BleakScanner(detection_callback=a._cb, service_uuids=[p.SERVICE_UUID],
                            bluez={"filters": {"DuplicateData": True}}):
        yield a


async def status(addr: str) -> p.Status:
    from bladetag.link import open_link

    async with open_link(addr) as link:
        return p.Status.parse(await link.read_status())


async def make_ready(addr: str) -> None:
    """READY and not connected, whatever the last test left behind."""
    from bladetag.link import open_link

    async with open_link(addr) as link:
        st = p.Status.parse(await link.read_status())
        if st.state != p.State.READY:
            await link.write_control(p.cmd_stop())
            for _ in range(50):
                await asyncio.sleep(0.1)
                if p.Status.parse(await link.read_status()).state == p.State.READY:
                    break
            else:
                raise AssertionError("tag didn't go READY after STOP")
