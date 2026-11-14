"""J-Link access for the hardware tests (tests/hw): read and write the tag's
RAM while it runs, and read its RTT log.

Uses SEGGER's JLinkExe / JLinkRTTLogger command-line tools, so there is no
extra Python dependency. Each call opens its own J-Link session (~0.5 s).
Connecting doesn't halt the CPU.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

DEVICE = "NRF54L15_M33"

# firmware/src/testhook.c
TESTHOOK_MAGIC = 0xB1AD7E57
CMD_NONE = 0
CMD_BUTTON_SHORT = 1
CMD_BUTTON_STOP_HOLD = 2


class JLinkError(RuntimeError):
    pass


def elf_symbol(elf: str | Path, name: str) -> int:
    """Address of ``name`` in ``elf`` (via binutils nm)."""
    out = subprocess.run(["nm", str(elf)], check=True, capture_output=True, text=True).stdout
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 3 and parts[2] == name:
            return int(parts[0], 16)
    raise KeyError(f"{name} not in {elf}")


class JLink:
    def __init__(self, serial: str | int, device: str = DEVICE, speed_khz: int = 4000):
        if shutil.which("JLinkExe") is None:
            raise JLinkError("JLinkExe not found (install the SEGGER J-Link software)")
        self.serial = str(serial)
        self.device = device
        self.speed = speed_khz

    def _base_args(self) -> list[str]:
        return ["-Device", self.device, "-If", "SWD", "-Speed", str(self.speed), "-USB", self.serial]

    def commander(self, commands: list[str]) -> str:
        with tempfile.NamedTemporaryFile("w", suffix=".jlink", delete=False) as f:
            f.write("\n".join(["connect", *commands, "q"]) + "\n")
            script = f.name
        try:
            r = subprocess.run(["JLinkExe", *self._base_args(), "-AutoConnect", "1", "-NoGui", "1",
                                "-ExitOnError", "1", "-CommandFile", script],
                               capture_output=True, text=True, timeout=30)
        finally:
            Path(script).unlink(missing_ok=True)
        out = r.stdout + r.stderr
        if r.returncode != 0 or "Cannot connect" in out or "Could not connect" in out:
            raise JLinkError(f"JLinkExe failed ({r.returncode}):\n{out[-1500:]}")
        return out

    def read32(self, addr: int, count: int = 1) -> list[int]:
        out = self.commander([f"mem32 0x{addr:08X} {count}"])
        words: list[int] = []
        for m in re.finditer(r"^([0-9A-Fa-f]{8}) = ((?:[0-9A-Fa-f]{8} ?)+)", out, re.M):
            words += [int(w, 16) for w in m.group(2).split()]
        if len(words) < count:
            raise JLinkError(f"couldn't read 0x{addr:08X}:\n{out[-800:]}")
        return words[:count]

    def write32(self, addr: int, value: int) -> None:
        self.commander([f"w4 0x{addr:08X} 0x{value & 0xFFFFFFFF:08X}"])

    def rtt_log(self, rtt_addr: int, seconds: float = 2.0) -> str:
        """Whatever the RTT up buffer (channel 0) holds, plus ``seconds`` of
        new output. The buffer is a ring: the start may be cut off."""
        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / "rtt.log"
            try:
                subprocess.run(["JLinkRTTLogger", *self._base_args(), "-RTTChannel", "0",
                                "-RTTAddress", f"0x{rtt_addr:08X}", str(log)],
                               capture_output=True, text=True, timeout=seconds)
            except subprocess.TimeoutExpired:
                pass
            return log.read_text(errors="replace") if log.exists() else ""


class TestHooks:
    """The RAM mailbox of a test build (firmware/test.conf)."""

    __test__ = False  # not a pytest class

    def __init__(self, jlink: JLink, elf: str | Path):
        self.jlink = jlink
        self.elf = Path(elf)
        self.addr = elf_symbol(self.elf, "blading_test")
        self.rtt_addr = elf_symbol(self.elf, "_SEGGER_RTT")

    def _read(self) -> tuple[int, int, int, int]:
        magic, cmd, done, result = self.jlink.read32(self.addr, 4)
        return magic, cmd, done, result - (1 << 32) if result & 0x80000000 else result

    def check(self) -> None:
        """Raise unless the tag runs the test build that ``elf`` came from."""
        magic = self._read()[0]
        if magic != TESTHOOK_MAGIC:
            raise JLinkError(
                f"no test mailbox at 0x{self.addr:08X} (read 0x{magic:08X}). The tag isn't "
                f"running the test build from {self.elf} (make build-test ota-test), or the "
                "J-Link isn't wired to the tag")

    def command(self, cmd: int, timeout: float = 3.0) -> None:
        _, pending, done, _ = self._read()
        if pending != CMD_NONE:
            raise JLinkError(f"command {pending} still pending")
        self.jlink.write32(self.addr + 4, cmd)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            _, pending, now_done, result = self._read()
            if pending == CMD_NONE and now_done != done:
                if result != 0:
                    raise JLinkError(f"command {cmd} failed: {result}")
                return
            time.sleep(0.05)
        raise JLinkError(f"command {cmd} not picked up within {timeout} s")

    def button_short(self) -> None:
        self.command(CMD_BUTTON_SHORT)

    def button_stop_hold(self) -> None:
        self.command(CMD_BUTTON_STOP_HOLD)

    def log(self, seconds: float = 2.0) -> str:
        return self.jlink.rtt_log(self.rtt_addr, seconds)
