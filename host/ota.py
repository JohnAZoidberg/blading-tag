#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["smpclient>=7,<8", "bleak>=0.22"]
# ///
"""Update a blading-tag over BLE (MCUboot + mcumgr SMP).

    uv run host/ota.py C0:BB:4A:A7:0D:57 firmware/build/firmware/zephyr/zephyr.signed.bin

Steps, each checked before going on:

1. upload the signed image to slot 1
2. check slot 1 now holds this image (by its SHA-512 from the image TLVs)
3. mark it for a *test* boot (not confirmed)
4. reset; MCUboot swaps it into slot 0
5. reconnect and check the new image runs and has confirmed itself

If the new firmware doesn't confirm itself, the next reset reverts to the old
one. BLE connects to the tag often fail the first time, so every step retries.
"""

import argparse
import asyncio
import struct
import sys
import time

from smpclient import SMPClient
from smpclient.generics import error, success
from smpclient.requests.image_management import ImageStatesRead, ImageStatesWrite
from smpclient.requests.os_management import ResetWrite
from smpclient.transport.ble import SMPBLETransport

IMAGE_MAGIC = 0x96F3B83D
TLV_INFO_MAGIC = 0x6907
TLV_PROT_INFO_MAGIC = 0x6908
TLV_SHA256, TLV_SHA384, TLV_SHA512 = 0x10, 0x11, 0x12


def image_info(data: bytes) -> tuple[str, bytes]:
    """Return (version, hash) of an MCUboot image, as img_mgmt reports them."""
    magic, _load, hdr_size, prot_tlv_size, img_size = struct.unpack_from("<IIHHI", data)
    if magic != IMAGE_MAGIC:
        sys.exit("not an MCUboot image (bad magic); use the *.signed.bin file")
    major, minor, rev, build = struct.unpack_from("<BBHI", data, 20)
    version = f"{major}.{minor}.{rev}" + (f".{build}" if build else "")

    off = hdr_size + img_size
    if prot_tlv_size:
        off += prot_tlv_size  # protected TLVs don't carry the hash
    tlv_magic, tlv_total = struct.unpack_from("<HH", data, off)
    if tlv_magic != TLV_INFO_MAGIC:
        sys.exit(f"unexpected TLV magic 0x{tlv_magic:04x}")
    end, off = off + tlv_total, off + 4
    while off < end:
        kind, length = struct.unpack_from("<BxH", data, off)
        if kind in (TLV_SHA256, TLV_SHA384, TLV_SHA512):
            return version, data[off + 4 : off + 4 + length]
        off += 4 + length
    sys.exit("no hash TLV in image")


async def connect(addr: str, attempts: int = 8) -> SMPClient:
    for i in range(1, attempts + 1):
        client = SMPClient(SMPBLETransport(), addr, timeout_s=10)
        try:
            await client.connect(20)
            return client
        except Exception as e:  # bleak raises a variety of errors here
            print(f"  connect attempt {i} failed: {e!r}")
            await asyncio.sleep(2)
    sys.exit(f"could not connect to {addr}")


async def states(client: SMPClient):
    r = await client.request(ImageStatesRead())
    if error(r):
        sys.exit(f"image state read failed: {r}")
    return {s.slot: s for s in r.images if (s.image or 0) == 0}


def show(st) -> None:
    for slot, s in sorted(st.items()):
        flags = [n for n in ("active", "confirmed", "pending", "permanent") if getattr(s, n)]
        print(f"  slot {slot}: {s.version:<12} {' '.join(flags)}")


async def run(addr: str, path: str, reboot_wait: float) -> None:
    data = open(path, "rb").read()
    version, img_hash = image_info(data)
    print(f"image {path}: version {version}, {len(data)} bytes")

    client = await connect(addr)
    st = await states(client)
    print("before:")
    show(st)
    if st.get(0) and bytes(st[0].hash) == img_hash:
        print("this image is already running")
        await client.disconnect()
        return

    # 1. upload, restarting from scratch if the link drops
    for attempt in range(3):
        try:
            t0, last = time.monotonic(), -1
            async for off in client.upload(data):
                pct = off * 100 // len(data)
                if pct // 10 != last // 10:
                    print(f"  upload {pct:3d}%", flush=True)
                    last = pct
            print(f"  uploaded in {time.monotonic() - t0:.1f} s")
            break
        except Exception as e:
            print(f"  upload failed: {e!r}, retrying")
            await client.disconnect()
            client = await connect(addr)
    else:
        sys.exit("upload failed")

    # 2. verify slot 1
    st = await states(client)
    if not st.get(1) or bytes(st[1].hash) != img_hash:
        show(st)
        sys.exit("slot 1 doesn't hold the uploaded image")

    # 3. mark for test boot
    r = await client.request(ImageStatesWrite(hash=img_hash))
    st = await states(client)
    if not st[1].pending:
        show(st)
        sys.exit(f"could not mark the image for test boot: {r}")
    print("  marked for test boot")

    # 4. reset
    try:
        await client.request(ResetWrite(), timeout_s=5)
    except Exception:
        pass  # the tag may drop the link before answering
    try:
        await client.disconnect()
    except Exception:
        pass
    print(f"  reset, waiting {reboot_wait:.0f} s for the swap")
    await asyncio.sleep(reboot_wait)

    # 5. verify the new image runs and confirmed itself
    client = await connect(addr)
    for _ in range(5):
        st = await states(client)
        if bytes(st[0].hash) == img_hash and st[0].confirmed:
            break
        await asyncio.sleep(2)
    print("after:")
    show(st)
    await client.disconnect()
    if bytes(st[0].hash) != img_hash:
        sys.exit("FAILED: the new image isn't running (rejected by MCUboot or reverted)")
    if not st[0].confirmed:
        sys.exit("FAILED: the new image runs but hasn't confirmed itself; "
                 "the next reset reverts it")
    print(f"OK: {version} is running and confirmed")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("address", help="tag Bluetooth address, e.g. C0:BB:4A:A7:0D:57")
    p.add_argument("image", help="signed app image (zephyr.signed.bin)")
    p.add_argument("--reboot-wait", type=float, default=8, help="seconds to wait after reset")
    a = p.parse_args()
    asyncio.run(run(a.address, a.image, a.reboot_wait))


if __name__ == "__main__":
    main()
