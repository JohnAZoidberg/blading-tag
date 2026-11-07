"""Connection to one tag. ``BleakLink`` talks to real hardware; the recorder
only uses the :class:`TagLink` interface so it can also run against
:class:`bladetag.faketag.FakeLink`."""

from __future__ import annotations

import abc
import asyncio
import re
from dataclasses import dataclass
from typing import Callable

from . import protocol as p

Callback = Callable[[bytes], None]


class ControlError(Exception):
    def __init__(self, code: int | None, msg: str = ""):
        self.code = code
        try:
            name = p.ErrorCode(code).name if code is not None else "unknown"
        except ValueError:
            name = f"0x{code:02x}"
        super().__init__(f"Control write failed: {name}" + (f" ({msg})" if msg else ""))


class TagLink(abc.ABC):
    address: str

    def __init__(self) -> None:
        self.on_disconnect: Callable[[], None] | None = None

    @abc.abstractmethod
    async def connect(self) -> None: ...

    @abc.abstractmethod
    async def disconnect(self) -> None: ...

    @property
    @abc.abstractmethod
    def is_connected(self) -> bool: ...

    @property
    @abc.abstractmethod
    def mtu(self) -> int: ...

    @abc.abstractmethod
    async def read_info(self) -> bytes: ...

    @abc.abstractmethod
    async def read_status(self) -> bytes: ...

    @abc.abstractmethod
    async def write_control(self, data: bytes) -> None:
        """Write with response. Raises :class:`ControlError` on an ATT error."""

    @abc.abstractmethod
    async def subscribe(self, on_status: Callback, on_data: Callback) -> None: ...

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, *exc):
        await self.disconnect()


# --- Scanning ----------------------------------------------------------------


@dataclass
class Found:
    address: str
    name: str | None
    rssi: int | None
    advert: p.Advert | None


async def scan(timeout: float = 5.0) -> list[Found]:
    from bleak import BleakScanner

    res = await BleakScanner.discover(timeout=timeout, return_adv=True, service_uuids=[p.SERVICE_UUID])
    out = []
    for dev, adv in res.values():
        if p.SERVICE_UUID not in [u.lower() for u in adv.service_uuids]:
            continue
        mfr = adv.manufacturer_data.get(p.COMPANY_ID)
        try:
            advert = p.Advert.parse(mfr) if mfr else None
        except (p.ProtocolError, ValueError):
            advert = None
        out.append(Found(dev.address, adv.local_name or dev.name, adv.rssi, advert))
    out.sort(key=lambda f: -(f.rssi or -999))
    return out


# --- bleak -------------------------------------------------------------------

_ATT_ERR = re.compile(r"(?:ATT error|Protocol Error|error)[^0-9]*0x([0-9a-fA-F]{2})")


class BleakLink(TagLink):
    def __init__(self, address: str, connect_timeout: float = 15.0):
        super().__init__()
        self.address = address
        self._timeout = connect_timeout
        self._client = None

    async def connect(self) -> None:
        from bleak import BleakClient, BleakScanner

        # Scanning first lets BlueZ/WinRT find a tag that isn't cached yet.
        dev = await BleakScanner.find_device_by_address(self.address, timeout=self._timeout)
        if dev is None:
            raise TimeoutError(f"{self.address} not found (asleep? connected elsewhere?)")
        self._client = BleakClient(dev, disconnected_callback=self._disconnected, timeout=self._timeout)
        await self._client.connect()
        backend = getattr(self._client, "_backend", None)
        # BlueZ only exchanges the MTU once something asks for it.
        acquire = getattr(backend, "_acquire_mtu", None)
        if acquire is not None:
            try:
                await acquire()
            except Exception:
                pass

    def _disconnected(self, _client) -> None:
        if self.on_disconnect:
            self.on_disconnect()

    async def disconnect(self) -> None:
        if self._client is not None:
            await self._client.disconnect()

    @property
    def is_connected(self) -> bool:
        return self._client is not None and self._client.is_connected

    @property
    def mtu(self) -> int:
        return self._client.mtu_size if self._client else 23

    async def read_info(self) -> bytes:
        return bytes(await self._client.read_gatt_char(p.INFO_UUID))

    async def read_status(self) -> bytes:
        return bytes(await self._client.read_gatt_char(p.STATUS_UUID))

    async def write_control(self, data: bytes) -> None:
        from bleak.exc import BleakError

        try:
            await self._client.write_gatt_char(p.CONTROL_UUID, data, response=True)
        except BleakError as e:
            m = _ATT_ERR.search(str(e))
            raise ControlError(int(m.group(1), 16) if m else None, str(e)) from e

    async def subscribe(self, on_status: Callback, on_data: Callback) -> None:
        await self._client.start_notify(p.STATUS_UUID, lambda _c, d: on_status(bytes(d)))
        await self._client.start_notify(p.DATA_UUID, lambda _c, d: on_data(bytes(d)))


def open_link(address: str) -> TagLink:
    if address.startswith("fake"):
        from .faketag import FakeLink, FakeTag

        return FakeLink(FakeTag.shared(address))
    return BleakLink(address)


async def with_link(address: str, fn):
    """Connect, run ``await fn(link)``, disconnect."""
    link = open_link(address)
    async with link:
        return await fn(link)


def run(coro):
    try:
        return asyncio.run(coro)
    except KeyboardInterrupt:
        pass
