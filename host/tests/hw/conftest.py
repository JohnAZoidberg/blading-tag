"""Fixtures for the hardware tests. See README.md here."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from bladetag import protocol as p
from hwutil import make_ready, watch_adverts

ADDR = os.environ.get("BLADETAG_HW")
JLINK = os.environ.get("BLADETAG_JLINK")
ELF = Path(os.environ.get(
    "BLADETAG_ELF",
    Path(__file__).resolve().parents[3] / "firmware/build-test/firmware/zephyr/zephyr.elf"))


def pytest_collection_modifyitems(config, items):
    if ADDR:
        return
    skip = pytest.mark.skip(reason="set BLADETAG_HW=<tag address> (and BLADETAG_JLINK) to run")
    for item in items:
        if Path(str(item.fspath)).parent == Path(__file__).parent:
            item.add_marker(skip)


@pytest.fixture(scope="session")
def hooks():
    from bladetag.jlink import JLink, TestHooks

    if not JLINK:
        pytest.skip("set BLADETAG_JLINK=<J-Link serial> (the tag must sit in the DK's DEBUG OUT)")
    if not ELF.exists():
        pytest.skip(f"{ELF} missing: make build-test, then make ota-test")
    h = TestHooks(JLink(JLINK), ELF)
    h.check()
    return h


@pytest.fixture(autouse=True)
def rtt_on_failure(request):
    """On failure, attach the tag's recent log to the report."""
    yield
    rep = getattr(request.node, "rep_call", None)
    if rep is not None and rep.failed and "hooks" in request.fixturenames:
        h = request.getfixturevalue("hooks")
        try:
            log = h.log(1.0)
        except Exception as e:  # noqa: BLE001
            log = f"(no RTT log: {e})"
        request.node.add_report_section("call", "tag RTT log", log[-4000:])


@pytest.hookimpl(wrapper=True, tryfirst=True)
def pytest_runtest_makereport(item, call):
    rep = yield
    setattr(item, f"rep_{rep.when}", rep)
    return rep


@pytest.fixture
def ready():
    """The tag is READY, nothing is connected, and it advertises READY."""
    async def go():
        await make_ready(ADDR)
        async with watch_adverts(ADDR) as a:
            await a.wait_state(p.State.READY, 10)

    asyncio.run(go())
    yield ADDR
    asyncio.run(make_ready(ADDR))
