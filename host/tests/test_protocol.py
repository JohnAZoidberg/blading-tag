"""Test vectors copied from PROTOCOL.md."""

import pytest

from bladetag import protocol as p


def h(s: str) -> bytes:
    return bytes.fromhex(s)


def test_uuids():
    assert p.SERVICE_UUID == "b1ad0001-0e83-479a-bb08-19dab75f76db"
    assert p.DATA_UUID == "b1ad0005-0e83-479a-bb08-19dab75f76db"


def test_local_name():
    assert p.local_name("C0:BB:4A:A7:0D:57") == "BladeTag-0D57"


def test_info_example():
    raw = h("01000100 3f000f1f 401f0000")
    info = p.Info.parse(raw)
    assert info.protocol_version == 1
    assert info.fw_string == "0.1.0"
    assert info.odr_hz == [25, 50, 100, 200, 400, 800]
    assert info.accel_ranges_g == [2, 4, 8, 16]
    assert info.gyro_ranges_dps == [125, 250, 500, 1000, 2000]
    assert info.buffer_capacity == 8000
    assert info.pack() == raw


def test_status_example():
    raw = h(
        "01 00 86 0b 03 00 c8 00 10 00 d0 07 70 17 00 00"
        "00 00 00 00 26 00 00 00 78 0a e3 05 00 00 00 00"
    )
    s = p.Status.parse(raw)
    assert s.state == p.State.RECORDING
    assert s.flags == 0
    assert s.battery_mv == 2950
    assert s.session_id == 3
    assert s.odr_hz == 200
    assert s.accel_range_g == 16
    assert s.position == p.Position.UNKNOWN
    assert s.gyro_range_dps == 2000
    assert s.samples_recorded == 6000
    assert s.samples_dropped == 0
    assert s.buffer_fill == 38
    assert s.tag_time_us == 98_765_432
    assert s.pack() == raw


def test_status_ignores_trailing_bytes():
    raw = bytes(32) + b"\xaa\xbb"
    assert p.Status.parse(raw).state == p.State.READY


def test_data_example():
    raw = h(
        "01 02 05 00 e8 03 00 00 4e 61 bc 00 00 00 00 00"
        "00 00 00 00 00 08 00 00 00 00 00 00"
        "64 00 9c ff 00 08 10 00 f0 ff 00 00"
    )
    pkt = p.DataPacket.parse(raw)
    assert (pkt.type, pkt.n, pkt.seq, pkt.sample_index, pkt.timestamp_us) == (1, 2, 5, 1000, 12_345_678)
    assert pkt.samples.tolist() == [[0, 0, 2048, 0, 0, 0], [100, -100, 2048, 16, -16, 0]]
    assert p.DataPacket.pack_imu6(5, 1000, 12_345_678, pkt.samples) == raw


def test_data_unknown_type_is_skippable():
    raw = bytes([0x7F, 3]) + bytes(14) + b"whatever"
    pkt = p.DataPacket.parse(raw)
    assert pkt.type == 0x7F and pkt.samples is None


def test_data_truncated():
    with pytest.raises(p.ProtocolError):
        p.DataPacket.parse(h("01 02 05 00 e8 03 00 00 4e 61 bc 00 00 00 00 00 00 00"))


def test_start_command():
    assert p.cmd_start(200, 16, 2000) == h("01 c8 00 10 d0 07")
    assert p.cmd_start() == h("01")


def test_samples_per_packet():
    assert p.samples_per_packet(247) == 19
    assert p.DATA_HEADER_SIZE + 19 * 12 == 244


def test_advert_roundtrip():
    a = p.Advert(1, p.State.READY, p.Position.LEFT, 3010)
    assert p.Advert.parse(a.pack()) == a


def test_units():
    assert 2048 * p.accel_scale_g(16) == pytest.approx(1.0)
    assert 16.384 * p.gyro_scale_dps(2000) == pytest.approx(1.0)
