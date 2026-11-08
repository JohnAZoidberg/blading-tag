/*
 * Protocol encoding, checked against the examples in PROTOCOL.md.
 */
#include <zephyr/ztest.h>

#include "proto.h"

ZTEST_SUITE(proto, NULL, NULL, NULL, NULL, NULL);

ZTEST(proto, test_info_matches_session_json_example)
{
	/* session.json example: "info_hex": "01000100 3f000f1f 401f0000" */
	static const uint8_t expect[] = {0x01, 0x00, 0x01, 0x00, 0x3f, 0x00,
					 0x0f, 0x1f, 0x40, 0x1f, 0x00, 0x00};
	struct proto_info info = {.fw_major = 0, .fw_minor = 1, .fw_patch = 0,
				  .buffer_capacity = 8000};
	uint8_t out[PROTO_INFO_LEN];

	zassert_equal(proto_encode_info(out, &info), sizeof(expect));
	zassert_mem_equal(out, expect, sizeof(expect));
}

ZTEST(proto, test_status_example)
{
	static const uint8_t expect[] = {
		0x01, 0x00, 0x86, 0x0b, 0x03, 0x00, 0xc8, 0x00, 0x10, 0x00, 0xd0,
		0x07, 0x70, 0x17, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x26, 0x00,
		0x00, 0x00, 0x78, 0x0a, 0xe3, 0x05, 0x00, 0x00, 0x00, 0x00,
	};
	struct proto_status st = {
		.state = PROTO_STATE_RECORDING,
		.battery_mv = 2950,
		.session_id = 3,
		.cfg = {200, 16, 2000},
		.position = PROTO_POS_UNKNOWN,
		.samples_recorded = 6000,
		.buffer_fill = 38,
		.tag_time_us = 98765432,
	};
	uint8_t out[PROTO_STATUS_LEN];

	zassert_equal(proto_encode_status(out, &st), sizeof(expect));
	zassert_mem_equal(out, expect, sizeof(expect));
}

ZTEST(proto, test_data_example)
{
	static const uint8_t expect[] = {
		0x01, 0x02, 0x05, 0x00, 0xe8, 0x03, 0x00, 0x00, 0x4e, 0x61, 0xbc, 0x00, 0x00, 0x00,
		0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x08, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
		0x64, 0x00, 0x9c, 0xff, 0x00, 0x08, 0x10, 0x00, 0xf0, 0xff, 0x00, 0x00,
	};
	static const struct imu6 s[] = {
		{0, 0, 2048, 0, 0, 0},
		{100, -100, 2048, 16, -16, 0},
	};
	uint8_t out[PROTO_MAX_DATA_LEN];

	zassert_equal(proto_encode_data(out, 5, 1000, 12345678, s, 2), sizeof(expect));
	zassert_mem_equal(out, expect, sizeof(expect));
}

ZTEST(proto, test_adv_mfg)
{
	static const uint8_t expect[] = {0xff, 0xff, 0x01, 0x01, 0x02, 0x86, 0x0b};
	uint8_t out[PROTO_ADV_MFG_LEN];

	zassert_equal(proto_encode_adv_mfg(out, PROTO_STATE_RECORDING, PROTO_POS_RIGHT_FOOT, 2950),
		      sizeof(expect));
	zassert_mem_equal(out, expect, sizeof(expect));
}

ZTEST(proto, test_start)
{
	static const uint8_t full[] = {0x01, 0xc8, 0x00, 0x10, 0xd0, 0x07};
	static const uint8_t bare[] = {0x01};
	static const uint8_t zeros[] = {0x01, 0x00, 0x00, 0x00, 0x00, 0x00};
	static const uint8_t custom[] = {0x01, 0x20, 0x03, 0x04, 0xf4, 0x01};
	struct proto_cmd cmd;

	zassert_ok(proto_parse_control(full, sizeof(full), &cmd));
	zassert_equal(cmd.op, PROTO_OP_START);
	zassert_equal(cmd.start.odr_hz, 200);
	zassert_equal(cmd.start.accel_range_g, 16);
	zassert_equal(cmd.start.gyro_range_dps, 2000);

	zassert_ok(proto_parse_control(bare, sizeof(bare), &cmd));
	zassert_equal(cmd.start.odr_hz, 200);
	zassert_equal(cmd.start.accel_range_g, 16);
	zassert_equal(cmd.start.gyro_range_dps, 2000);

	zassert_ok(proto_parse_control(zeros, sizeof(zeros), &cmd));
	zassert_equal(cmd.start.odr_hz, 200);

	zassert_ok(proto_parse_control(custom, sizeof(custom), &cmd));
	zassert_equal(cmd.start.odr_hz, 800);
	zassert_equal(cmd.start.accel_range_g, 4);
	zassert_equal(cmd.start.gyro_range_dps, 500);
}

ZTEST(proto, test_control_errors)
{
	struct proto_cmd cmd;

	zassert_equal(proto_parse_control(NULL, 0, &cmd), PROTO_ERR_BAD_LENGTH);
	zassert_equal(proto_parse_control((uint8_t[]){0x01, 0xc8, 0x00}, 3, &cmd),
		      PROTO_ERR_BAD_LENGTH);
	/* 300 Hz, 3 g, 300 dps aren't supported. */
	zassert_equal(proto_parse_control((uint8_t[]){0x01, 0x2c, 0x01, 0, 0, 0}, 6, &cmd),
		      PROTO_ERR_BAD_PARAM);
	zassert_equal(proto_parse_control((uint8_t[]){0x01, 0, 0, 3, 0, 0}, 6, &cmd),
		      PROTO_ERR_BAD_PARAM);
	zassert_equal(proto_parse_control((uint8_t[]){0x01, 0, 0, 0, 0x2c, 0x01}, 6, &cmd),
		      PROTO_ERR_BAD_PARAM);
	zassert_equal(proto_parse_control((uint8_t[]){0x02, 0x00}, 2, &cmd),
		      PROTO_ERR_BAD_LENGTH);
	zassert_equal(proto_parse_control((uint8_t[]){0x03}, 1, &cmd), PROTO_ERR_BAD_LENGTH);
	zassert_equal(proto_parse_control((uint8_t[]){0x04, 8}, 2, &cmd), PROTO_ERR_BAD_PARAM);
	zassert_equal(proto_parse_control((uint8_t[]){0x05, 1}, 2, &cmd), PROTO_ERR_BAD_LENGTH);
	zassert_equal(proto_parse_control((uint8_t[]){0x42}, 1, &cmd),
		      PROTO_ERR_UNKNOWN_OPCODE);

	zassert_ok(proto_parse_control((uint8_t[]){0x02}, 1, &cmd));
	zassert_ok(proto_parse_control((uint8_t[]){0x03, 10}, 2, &cmd));
	zassert_equal(cmd.identify_s, 10);
	zassert_ok(proto_parse_control((uint8_t[]){0x04, 2}, 2, &cmd));
	zassert_equal(cmd.position, PROTO_POS_RIGHT_FOOT);
	zassert_ok(proto_parse_control((uint8_t[]){0x04, 7}, 2, &cmd));
	zassert_equal(cmd.position, PROTO_POS_BACKPACK);
	zassert_ok(proto_parse_control((uint8_t[]){0x05}, 1, &cmd));
}

ZTEST(proto, test_samples_per_packet)
{
	zassert_equal(proto_samples_per_packet(247), 19);
	zassert_equal(proto_samples_per_packet(517), 19);
	zassert_equal(proto_samples_per_packet(64), 3);
	zassert_equal(proto_samples_per_packet(23), 0);
	zassert_equal(proto_samples_per_packet(0), 0);
}
