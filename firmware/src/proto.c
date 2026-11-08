#include "proto.h"

#include <string.h>
#include <zephyr/sys/byteorder.h>
#include <zephyr/sys/util.h>

static const uint16_t odr_values[] = {25, 50, 100, 200, 400, 800};
static const uint8_t accel_values[] = {2, 4, 8, 16};
static const uint16_t gyro_values[] = {125, 250, 500, 1000, 2000};

uint16_t proto_odr_mask(void)
{
	return BIT_MASK(ARRAY_SIZE(odr_values));
}

uint8_t proto_accel_range_mask(void)
{
	return BIT_MASK(ARRAY_SIZE(accel_values));
}

uint8_t proto_gyro_range_mask(void)
{
	return BIT_MASK(ARRAY_SIZE(gyro_values));
}

bool proto_odr_valid(uint16_t hz)
{
	for (size_t i = 0; i < ARRAY_SIZE(odr_values); i++) {
		if (odr_values[i] == hz) {
			return true;
		}
	}
	return false;
}

bool proto_accel_range_valid(uint8_t g)
{
	for (size_t i = 0; i < ARRAY_SIZE(accel_values); i++) {
		if (accel_values[i] == g) {
			return true;
		}
	}
	return false;
}

bool proto_gyro_range_valid(uint16_t dps)
{
	for (size_t i = 0; i < ARRAY_SIZE(gyro_values); i++) {
		if (gyro_values[i] == dps) {
			return true;
		}
	}
	return false;
}

int proto_parse_control(const uint8_t *buf, size_t len, struct proto_cmd *cmd)
{
	if (len < 1) {
		return PROTO_ERR_BAD_LENGTH;
	}

	memset(cmd, 0, sizeof(*cmd));
	cmd->op = buf[0];
	buf++;
	len--;

	switch (cmd->op) {
	case PROTO_OP_START: {
		struct proto_rec_cfg *c = &cmd->start;

		if (len != 0 && len != 5) {
			return PROTO_ERR_BAD_LENGTH;
		}
		if (len == 5) {
			c->odr_hz = sys_get_le16(&buf[0]);
			c->accel_range_g = buf[2];
			c->gyro_range_dps = sys_get_le16(&buf[3]);
		}
		if (c->odr_hz == 0) {
			c->odr_hz = PROTO_DEFAULT_ODR_HZ;
		}
		if (c->accel_range_g == 0) {
			c->accel_range_g = PROTO_DEFAULT_ACCEL_RANGE_G;
		}
		if (c->gyro_range_dps == 0) {
			c->gyro_range_dps = PROTO_DEFAULT_GYRO_RANGE_DPS;
		}
		if (!proto_odr_valid(c->odr_hz) || !proto_accel_range_valid(c->accel_range_g) ||
		    !proto_gyro_range_valid(c->gyro_range_dps)) {
			return PROTO_ERR_BAD_PARAM;
		}
		return 0;
	}
	case PROTO_OP_STOP:
	case PROTO_OP_SLEEP:
		return len == 0 ? 0 : PROTO_ERR_BAD_LENGTH;
	case PROTO_OP_IDENTIFY:
		if (len != 1) {
			return PROTO_ERR_BAD_LENGTH;
		}
		cmd->identify_s = buf[0];
		return 0;
	case PROTO_OP_SET_POSITION:
		if (len != 1) {
			return PROTO_ERR_BAD_LENGTH;
		}
		if (buf[0] > PROTO_POS_LAST) {
			return PROTO_ERR_BAD_PARAM;
		}
		cmd->position = buf[0];
		return 0;
	default:
		return PROTO_ERR_UNKNOWN_OPCODE;
	}
}

size_t proto_encode_info(uint8_t out[PROTO_INFO_LEN], const struct proto_info *info)
{
	out[0] = PROTO_VERSION;
	out[1] = info->fw_major;
	out[2] = info->fw_minor;
	out[3] = info->fw_patch;
	sys_put_le16(proto_odr_mask(), &out[4]);
	out[6] = proto_accel_range_mask();
	out[7] = proto_gyro_range_mask();
	sys_put_le32(info->buffer_capacity, &out[8]);
	return PROTO_INFO_LEN;
}

size_t proto_encode_status(uint8_t out[PROTO_STATUS_LEN], const struct proto_status *st)
{
	out[0] = st->state;
	out[1] = st->flags;
	sys_put_le16(st->battery_mv, &out[2]);
	sys_put_le16(st->session_id, &out[4]);
	sys_put_le16(st->cfg.odr_hz, &out[6]);
	out[8] = st->cfg.accel_range_g;
	out[9] = st->position;
	sys_put_le16(st->cfg.gyro_range_dps, &out[10]);
	sys_put_le32(st->samples_recorded, &out[12]);
	sys_put_le32(st->samples_dropped, &out[16]);
	sys_put_le32(st->buffer_fill, &out[20]);
	sys_put_le64(st->tag_time_us, &out[24]);
	return PROTO_STATUS_LEN;
}

size_t proto_encode_adv_mfg(uint8_t out[PROTO_ADV_MFG_LEN], uint8_t state, uint8_t position,
			    uint16_t battery_mv)
{
	sys_put_le16(PROTO_COMPANY_ID, &out[0]);
	out[2] = PROTO_VERSION;
	out[3] = state;
	out[4] = position;
	sys_put_le16(battery_mv, &out[5]);
	return PROTO_ADV_MFG_LEN;
}

size_t proto_encode_data(uint8_t *out, uint16_t seq, uint32_t sample_index, uint64_t timestamp_us,
			 const struct imu6 *samples, uint8_t n)
{
	uint8_t *p = &out[PROTO_DATA_HDR_LEN];

	out[0] = PROTO_PKT_IMU6;
	out[1] = n;
	sys_put_le16(seq, &out[2]);
	sys_put_le32(sample_index, &out[4]);
	sys_put_le64(timestamp_us, &out[8]);

	for (uint8_t i = 0; i < n; i++) {
		const struct imu6 *s = &samples[i];

		sys_put_le16((uint16_t)s->ax, &p[0]);
		sys_put_le16((uint16_t)s->ay, &p[2]);
		sys_put_le16((uint16_t)s->az, &p[4]);
		sys_put_le16((uint16_t)s->gx, &p[6]);
		sys_put_le16((uint16_t)s->gy, &p[8]);
		sys_put_le16((uint16_t)s->gz, &p[10]);
		p += PROTO_SAMPLE_LEN;
	}

	return PROTO_DATA_HDR_LEN + (size_t)n * PROTO_SAMPLE_LEN;
}

uint8_t proto_samples_per_packet(uint16_t mtu)
{
	if (mtu < 3 + PROTO_DATA_HDR_LEN + PROTO_SAMPLE_LEN) {
		return 0;
	}

	uint32_t n = (mtu - 3 - PROTO_DATA_HDR_LEN) / PROTO_SAMPLE_LEN;

	return MIN(n, PROTO_MAX_SAMPLES_PER_PACKET);
}
