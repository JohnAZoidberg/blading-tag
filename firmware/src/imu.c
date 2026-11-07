/*
 * BMI270 FIFO reader.
 *
 * Zephyr's BMI270 driver does the chip init and the config-file upload at
 * boot. It only offers per-sample data-ready triggers, which can miss samples
 * without noticing and wake the CPU at the full sample rate. So once the chip
 * is up, this module drives the FIFO through the driver's register accessors:
 *
 * - Headerless FIFO with accel + gyro: fixed 12-byte frames, gyro then accel.
 * - The watermark interrupt (INT1, P1.04) fires every ~100 ms. Its edge is
 *   timestamped in the ISR and belongs to the W-th frame in the FIFO, which
 *   gives each batch's start time without per-sample interrupts.
 * - A poll timeout backs up the interrupt, and a full FIFO (overflow) is
 *   detected and the lost samples are counted, so sample_index stays exact.
 */
#include "imu.h"

#include <string.h>
#include <zephyr/device.h>
#include <zephyr/drivers/gpio.h>
#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>
#include <zephyr/sys/byteorder.h>

LOG_MODULE_REGISTER(imu, LOG_LEVEL_INF);

/*
 * Exported by Zephyr's BMI270 driver (drivers/sensor/bosch/bmi270/bmi270.h,
 * which is private to the driver, so the prototypes are repeated here).
 */
int bmi270_reg_read(const struct device *dev, uint8_t reg, uint8_t *data, uint16_t length);
int bmi270_reg_write(const struct device *dev, uint8_t reg, const uint8_t *data, uint16_t length);

#define REG_CHIP_ID       0x00
#define REG_ERR           0x02
#define REG_FIFO_LENGTH_0 0x24
#define REG_FIFO_DATA     0x26
#define REG_ACC_CONF      0x40
#define REG_ACC_RANGE     0x41
#define REG_GYR_CONF      0x42
#define REG_GYR_RANGE     0x43
#define REG_FIFO_DOWNS    0x45
#define REG_FIFO_WTM_0    0x46
#define REG_FIFO_CONFIG_0 0x48
#define REG_FIFO_CONFIG_1 0x49
#define REG_INT1_IO_CTRL  0x53
#define REG_INT_LATCH     0x55
#define REG_INT_MAP_DATA  0x58
#define REG_PWR_CONF      0x7C
#define REG_PWR_CTRL      0x7D
#define REG_CMD           0x7E

#define CHIP_ID 0x24

#define ACC_CONF_BWP_NORM_AVG4 (0x2 << 4)
#define ACC_CONF_FILTER_PERF   BIT(7)
#define GYR_CONF_BWP_NORM      (0x2 << 4)
#define GYR_CONF_NOISE_PERF    BIT(6)
#define GYR_CONF_FILTER_PERF   BIT(7)
#define FIFO_DOWNS_FILTERED    0x88 /* filtered data, no downsampling (reset value) */
#define FIFO_CONFIG_1_ACC_GYR  (BIT(6) | BIT(7)) /* header_en (bit 4) cleared */
#define INT_IO_OUTPUT_EN       BIT(3)
#define INT_IO_ACTIVE_HIGH     BIT(1)
#define INT_MAP_FWM_INT1       BIT(1)
#define PWR_CONF_APS           BIT(0)
#define PWR_CONF_FIFO_SELF_WKUP BIT(1)
#define PWR_CTRL_GYR           BIT(1)
#define PWR_CTRL_ACC           BIT(2)
#define CMD_FIFO_FLUSH         0xB0

#define FIFO_BYTES  2048
#define FRAME_BYTES 12
#define MAX_FRAMES  (FIFO_BYTES / FRAME_BYTES)

/* Gyro start-up from suspend is 45 ms; these samples are discarded. */
#define STARTUP_MS 55

static const struct device *const dev = DEVICE_DT_GET(DT_NODELABEL(bmi270));
static const struct gpio_dt_spec int1 = GPIO_DT_SPEC_GET(DT_NODELABEL(bmi270), irq_gpios);
static struct gpio_callback int1_cb;

static imu_batch_cb_t batch_cb;
static bool present;

static K_SEM_DEFINE(wake_sem, 0, 1);
static K_SEM_DEFINE(stopped_sem, 0, 1);
static atomic_t running;
static atomic_t stop_req;

static struct k_spinlock irq_lock;
static bool irq_pending;
static uint64_t irq_t_us;

/* Owned by the IMU thread while running, by imu_start() otherwise. */
static struct {
	uint16_t odr_hz;
	uint32_t wtm_frames;
	uint32_t nominal_ns;
	uint32_t period_ns;
	uint32_t next_index;
	bool have_last;
	uint32_t last_index;
	uint64_t last_t_us;
	bool have_ref;
	uint32_t ref_index;
	uint64_t ref_t_us;
} st;

static atomic_t period_ns_shared = ATOMIC_INIT(5000000);

static uint8_t fifo_buf[MAX_FRAMES * FRAME_BYTES];
static struct imu6 samples[MAX_FRAMES];

uint64_t tag_time_us(void)
{
	return k_cyc_to_us_floor64(k_cycle_get_64());
}

uint32_t imu_period_ns(void)
{
	return (uint32_t)atomic_get(&period_ns_shared);
}

static int wr(uint8_t reg, uint8_t val)
{
	int err = bmi270_reg_write(dev, reg, &val, 1);

	/* 2 us between writes in normal mode; be generous. */
	k_busy_wait(5);
	return err;
}

static void int1_isr(const struct device *port, struct gpio_callback *cb, uint32_t pins)
{
	k_spinlock_key_t key = k_spin_lock(&irq_lock);

	irq_t_us = tag_time_us();
	irq_pending = true;
	k_spin_unlock(&irq_lock, key);
	k_sem_give(&wake_sem);
}

static uint8_t odr_bits(uint16_t hz)
{
	switch (hz) {
	case 25:
		return 0x06;
	case 50:
		return 0x07;
	case 100:
		return 0x08;
	case 200:
		return 0x09;
	case 400:
		return 0x0A;
	default:
		return 0x0B; /* 800 */
	}
}

static uint8_t acc_range_bits(uint8_t g)
{
	switch (g) {
	case 2:
		return 0;
	case 4:
		return 1;
	case 8:
		return 2;
	default:
		return 3; /* 16 g */
	}
}

static uint8_t gyr_range_bits(uint16_t dps)
{
	switch (dps) {
	case 125:
		return 4;
	case 250:
		return 3;
	case 500:
		return 2;
	case 1000:
		return 1;
	default:
		return 0; /* 2000 dps */
	}
}

static uint64_t frames_us(uint32_t frames, uint32_t period_ns)
{
	return ((uint64_t)frames * period_ns + 500) / 1000;
}

/* Sensors off, FIFO off, advanced power save on: ~3.5 uA suspend. */
static void power_down(void)
{
	gpio_pin_interrupt_configure_dt(&int1, GPIO_INT_DISABLE);
	wr(REG_INT_MAP_DATA, 0);
	wr(REG_PWR_CTRL, 0);
	wr(REG_FIFO_CONFIG_1, 0);
	wr(REG_CMD, CMD_FIFO_FLUSH);
	wr(REG_PWR_CONF, PWR_CONF_APS | PWR_CONF_FIFO_SELF_WKUP);
}

static void read_fifo(bool final)
{
	uint8_t lenb[2];
	bool irq;
	uint64_t t_irq;

	/*
	 * Take the IRQ flag before reading the fill level, so a watermark
	 * edge we count is always for a frame this read includes.
	 */
	k_spinlock_key_t key = k_spin_lock(&irq_lock);

	irq = irq_pending;
	t_irq = irq_t_us;
	irq_pending = false;
	k_spin_unlock(&irq_lock, key);

	if (bmi270_reg_read(dev, REG_FIFO_LENGTH_0, lenb, 2) != 0) {
		LOG_WRN("FIFO length read failed");
		return;
	}

	uint64_t now = tag_time_us();
	uint32_t n = (sys_get_le16(lenb) & 0x3FFF) / FRAME_BYTES;

	if (n == 0) {
		return;
	}

	/* With stop_on_full off, a full FIFO overwrites its oldest frames. */
	bool overflow = n >= MAX_FRAMES - 1;

	n = MIN(n, MAX_FRAMES);

	if (bmi270_reg_read(dev, REG_FIFO_DATA, fifo_buf, n * FRAME_BYTES) != 0) {
		LOG_WRN("FIFO data read failed");
		return;
	}

	for (uint32_t i = 0; i < n; i++) {
		const uint8_t *f = &fifo_buf[i * FRAME_BYTES];

		samples[i].gx = (int16_t)sys_get_le16(&f[0]);
		samples[i].gy = (int16_t)sys_get_le16(&f[2]);
		samples[i].gz = (int16_t)sys_get_le16(&f[4]);
		samples[i].ax = (int16_t)sys_get_le16(&f[6]);
		samples[i].ay = (int16_t)sys_get_le16(&f[8]);
		samples[i].az = (int16_t)sys_get_le16(&f[10]);
	}

	uint32_t p = st.period_ns;
	uint64_t predicted = 0;
	uint64_t t_first;
	uint32_t lost = 0;
	bool timed = false;

	if (st.have_last) {
		predicted = st.last_t_us + frames_us(st.next_index - st.last_index, p);
	}

	if (overflow) {
		/* The newest frame was taken about now. */
		t_first = now - frames_us(n - 1, p);
		if (st.have_last && t_first > predicted) {
			lost = (uint32_t)(((t_first - predicted) * 1000 + p / 2) / p);
		}
		LOG_WRN("FIFO overflow, ~%u samples lost", lost);
	} else if (irq && n >= st.wtm_frames && !final) {
		/* The watermark edge is the W-th frame in the FIFO. */
		t_first = t_irq - frames_us(st.wtm_frames - 1, p);
		timed = true;
		if (st.have_last) {
			int64_t err_us = (int64_t)(t_first - predicted);

			/* An edge that doesn't fit is from a race; ignore it. */
			if (err_us > (int64_t)frames_us(3, p) || err_us < -(int64_t)frames_us(3, p)) {
				t_first = predicted;
				timed = false;
			}
		}
	} else if (st.have_last) {
		t_first = predicted;
	} else {
		t_first = now - frames_us(n - 1, p);
	}

	uint32_t index = st.next_index + lost;

	batch_cb(samples, n, index, t_first, lost);

	st.next_index = index + n;
	st.last_index = index;
	st.last_t_us = t_first;
	st.have_last = true;

	/* Fit the true sample period from IRQ-timed batches. */
	if (timed) {
		if (!st.have_ref) {
			st.have_ref = true;
			st.ref_index = index;
			st.ref_t_us = t_first;
		} else if (index - st.ref_index >= 2u * st.odr_hz) {
			uint64_t est = ((t_first - st.ref_t_us) * 1000) / (index - st.ref_index);
			uint32_t lo = st.nominal_ns - st.nominal_ns / 20;
			uint32_t hi = st.nominal_ns + st.nominal_ns / 20;

			st.period_ns = CLAMP(est, lo, hi);
			atomic_set(&period_ns_shared, st.period_ns);
		}
	}
}

static void imu_thread(void *a, void *b, void *c)
{
	for (;;) {
		if (!atomic_get(&running)) {
			k_sem_take(&wake_sem, K_FOREVER);
			continue;
		}

		/* Backstop in case a watermark edge is missed. */
		k_timeout_t poll = K_USEC(frames_us(3 * st.wtm_frames, st.nominal_ns));

		k_sem_take(&wake_sem, poll);

		if (atomic_get(&stop_req)) {
			read_fifo(true);
			power_down();
			atomic_set(&running, 0);
			atomic_set(&stop_req, 0);
			k_sem_give(&stopped_sem);
			continue;
		}

		read_fifo(false);
	}
}

K_THREAD_DEFINE(imu_tid, 2048, imu_thread, NULL, NULL, NULL, 2, 0, 0);

int imu_init(imu_batch_cb_t cb)
{
	uint8_t id = 0;

	batch_cb = cb;

	if (!device_is_ready(dev)) {
		LOG_ERR("BMI270 not ready (init failed)");
		return -ENODEV;
	}
	if (bmi270_reg_read(dev, REG_CHIP_ID, &id, 1) != 0 || id != CHIP_ID) {
		LOG_ERR("BMI270 chip id 0x%02x", id);
		return -EIO;
	}
	if (!gpio_is_ready_dt(&int1)) {
		return -ENODEV;
	}

	gpio_pin_configure_dt(&int1, GPIO_INPUT);
	gpio_init_callback(&int1_cb, int1_isr, BIT(int1.pin));
	gpio_add_callback_dt(&int1, &int1_cb);

	present = true;
	LOG_INF("BMI270 ready");
	return 0;
}

int imu_start(const struct proto_rec_cfg *cfg)
{
	uint8_t odr = odr_bits(cfg->odr_hz);
	uint8_t id = 0;
	int err = 0;

	if (!present || atomic_get(&running)) {
		return -EBUSY;
	}

	memset(&st, 0, sizeof(st));
	st.odr_hz = cfg->odr_hz;
	st.nominal_ns = 1000000000u / cfg->odr_hz;
	st.period_ns = st.nominal_ns;
	/* A batch every ~100 ms. */
	st.wtm_frames = CLAMP(cfg->odr_hz / 10, 2, 100);
	atomic_set(&period_ns_shared, st.period_ns);

	/* Leave advanced power save first; writes need 450 us gaps until then. */
	k_usleep(500);
	err |= wr(REG_PWR_CONF, 0);
	k_usleep(500);

	err |= bmi270_reg_read(dev, REG_CHIP_ID, &id, 1);
	if (err || id != CHIP_ID) {
		LOG_ERR("BMI270 not responding (id 0x%02x)", id);
		power_down();
		return -EIO;
	}

	err |= wr(REG_INT_MAP_DATA, 0);
	err |= wr(REG_PWR_CTRL, 0);
	err |= wr(REG_FIFO_CONFIG_1, 0);
	err |= wr(REG_ACC_CONF, odr | ACC_CONF_BWP_NORM_AVG4 | ACC_CONF_FILTER_PERF);
	err |= wr(REG_ACC_RANGE, acc_range_bits(cfg->accel_range_g));
	err |= wr(REG_GYR_CONF, odr | GYR_CONF_BWP_NORM | GYR_CONF_NOISE_PERF | GYR_CONF_FILTER_PERF);
	err |= wr(REG_GYR_RANGE, gyr_range_bits(cfg->gyro_range_dps));
	err |= wr(REG_FIFO_DOWNS, FIFO_DOWNS_FILTERED);
	err |= wr(REG_FIFO_CONFIG_0, 0);
	err |= wr(REG_FIFO_CONFIG_1, FIFO_CONFIG_1_ACC_GYR);

	uint16_t wtm = st.wtm_frames * FRAME_BYTES;
	uint8_t wtm_b[2] = {wtm & 0xFF, (wtm >> 8) & 0x1F};

	err |= bmi270_reg_write(dev, REG_FIFO_WTM_0, wtm_b, 2);
	err |= wr(REG_INT1_IO_CTRL, INT_IO_OUTPUT_EN | INT_IO_ACTIVE_HIGH);
	err |= wr(REG_INT_LATCH, 0);
	err |= wr(REG_PWR_CTRL, PWR_CTRL_ACC | PWR_CTRL_GYR);

	k_msleep(MAX(STARTUP_MS, 3 * 1000 / cfg->odr_hz));

	uint8_t errreg = 0;

	err |= bmi270_reg_read(dev, REG_ERR, &errreg, 1);
	if (err || (errreg & BIT(0))) {
		LOG_ERR("BMI270 start failed (err %d, ERR_REG 0x%02x)", err, errreg);
		power_down();
		return -EIO;
	}

	/*
	 * Drop the start-up samples. The next frame is sample 0. The watermark
	 * interrupt isn't mapped yet, so no stale edge can be pending after this.
	 */
	err |= wr(REG_CMD, CMD_FIFO_FLUSH);

	k_spinlock_key_t key = k_spin_lock(&irq_lock);

	irq_pending = false;
	k_spin_unlock(&irq_lock, key);

	err |= gpio_pin_interrupt_configure_dt(&int1, GPIO_INT_EDGE_TO_ACTIVE);
	err |= wr(REG_INT_MAP_DATA, INT_MAP_FWM_INT1);
	if (err) {
		power_down();
		return -EIO;
	}

	atomic_set(&stop_req, 0);
	atomic_set(&running, 1);
	k_sem_give(&wake_sem);

	LOG_INF("started: %u Hz, %u g, %u dps, watermark %u frames", cfg->odr_hz,
		cfg->accel_range_g, cfg->gyro_range_dps, st.wtm_frames);
	return 0;
}

void imu_stop(void)
{
	if (!atomic_get(&running)) {
		return;
	}

	k_sem_reset(&stopped_sem);
	atomic_set(&stop_req, 1);
	k_sem_give(&wake_sem);
	k_sem_take(&stopped_sem, K_FOREVER);
	LOG_INF("stopped after %u samples", st.next_index);
}
