/*
 * BLE protocol v1 encoding (PROTOCOL.md section 1).
 *
 * Pure functions with no Bluetooth or kernel dependencies, so they can be
 * unit tested on native_sim (firmware/tests/unit).
 */
#ifndef BLADING_PROTO_H_
#define BLADING_PROTO_H_

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define PROTO_VERSION    1
#define PROTO_COMPANY_ID 0xFFFF

/* b1adXXXX-0e83-479a-bb08-19dab75f76db, for BT_UUID_128_ENCODE() */
#define PROTO_UUID_W32(short16) (0xb1ad0000u | (short16))
#define PROTO_UUID_W1           0x0e83
#define PROTO_UUID_W2           0x479a
#define PROTO_UUID_W3           0xbb08
#define PROTO_UUID_W48          0x19dab75f76dbULL

#define PROTO_INFO_LEN     12
#define PROTO_STATUS_LEN   32
#define PROTO_ADV_MFG_LEN  7 /* including the company ID */
#define PROTO_DATA_HDR_LEN 16
#define PROTO_SAMPLE_LEN   12
#define PROTO_MIN_MTU      64

/* The tag supports an ATT MTU of 247, so at most 19 samples per packet. */
#define PROTO_MAX_SAMPLES_PER_PACKET 19
#define PROTO_MAX_DATA_LEN (PROTO_DATA_HDR_LEN + PROTO_MAX_SAMPLES_PER_PACKET * PROTO_SAMPLE_LEN)

#define PROTO_PKT_IMU6 0x01

enum proto_state {
	PROTO_STATE_READY = 0,
	PROTO_STATE_RECORDING = 1,
};

/* Where on the body the tag is worn. */
enum proto_position {
	PROTO_POS_UNKNOWN = 0,
	PROTO_POS_LEFT_FOOT = 1,
	PROTO_POS_RIGHT_FOOT = 2,
	PROTO_POS_LEFT_WRIST = 3,
	PROTO_POS_RIGHT_WRIST = 4,
	PROTO_POS_LEFT_POCKET = 5,
	PROTO_POS_RIGHT_POCKET = 6,
	PROTO_POS_BACKPACK = 7,
	PROTO_POS_LAST = PROTO_POS_BACKPACK,
};

enum proto_opcode {
	PROTO_OP_START = 0x01,
	PROTO_OP_STOP = 0x02,
	PROTO_OP_IDENTIFY = 0x03,
	PROTO_OP_SET_POSITION = 0x04,
	PROTO_OP_SLEEP = 0x05,
};

/* ATT application error codes returned from Control writes. */
enum proto_err {
	PROTO_ERR_UNKNOWN_OPCODE = 0x80,
	PROTO_ERR_BAD_LENGTH = 0x81,
	PROTO_ERR_BAD_PARAM = 0x82,
	PROTO_ERR_BAD_STATE = 0x83,
	PROTO_ERR_SENSOR_FAULT = 0x84,
};

enum proto_status_flag {
	PROTO_FLAG_IMU_FAULT = 1 << 0,
	PROTO_FLAG_OVERFLOW = 1 << 1,
	PROTO_FLAG_LOW_BATTERY = 1 << 2,
};

#define PROTO_DEFAULT_ODR_HZ         200
#define PROTO_DEFAULT_ACCEL_RANGE_G  16
#define PROTO_DEFAULT_GYRO_RANGE_DPS 2000

/* One IMU6 sample in protocol order. Raw BMI270 counts. */
struct imu6 {
	int16_t ax, ay, az;
	int16_t gx, gy, gz;
};

struct proto_rec_cfg {
	uint16_t odr_hz;
	uint8_t accel_range_g;
	uint16_t gyro_range_dps;
};

struct proto_cmd {
	uint8_t op;
	union {
		struct proto_rec_cfg start;
		uint8_t identify_s;
		uint8_t position;
	};
};

struct proto_info {
	uint8_t fw_major, fw_minor, fw_patch;
	uint32_t buffer_capacity;
};

struct proto_status {
	uint8_t state;
	uint8_t flags;
	uint16_t battery_mv;
	uint16_t session_id;
	struct proto_rec_cfg cfg;
	uint8_t position;
	uint32_t samples_recorded;
	uint32_t samples_dropped;
	uint32_t buffer_fill;
	uint64_t tag_time_us;
};

/* Supported values, as bit masks for Info (bit n = n-th value in the list). */
uint16_t proto_odr_mask(void);
uint8_t proto_accel_range_mask(void);
uint8_t proto_gyro_range_mask(void);

bool proto_odr_valid(uint16_t hz);
bool proto_accel_range_valid(uint8_t g);
bool proto_gyro_range_valid(uint16_t dps);

/*
 * Parse a Control write. Checks the length and the parameter values, and
 * fills in defaults for START. State checks are the caller's job.
 *
 * Returns 0 or a PROTO_ERR_* code.
 */
int proto_parse_control(const uint8_t *buf, size_t len, struct proto_cmd *cmd);

size_t proto_encode_info(uint8_t out[PROTO_INFO_LEN], const struct proto_info *info);
size_t proto_encode_status(uint8_t out[PROTO_STATUS_LEN], const struct proto_status *st);
size_t proto_encode_adv_mfg(uint8_t out[PROTO_ADV_MFG_LEN], uint8_t state, uint8_t position,
			    uint16_t battery_mv);

/* Writes the 16-byte header and n samples; returns the packet length. */
size_t proto_encode_data(uint8_t *out, uint16_t seq, uint32_t sample_index, uint64_t timestamp_us,
			 const struct imu6 *samples, uint8_t n);

/* Samples per Data packet for an ATT MTU, capped at PROTO_MAX_SAMPLES_PER_PACKET. */
uint8_t proto_samples_per_packet(uint16_t mtu);

#endif /* BLADING_PROTO_H_ */
