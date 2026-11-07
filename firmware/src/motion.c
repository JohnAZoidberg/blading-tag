/*
 * ADXL367 wake-on-motion.
 *
 * The ADXL367 runs in wake-up mode (~6 Hz, ~0.2 uA) with referenced activity
 * detection on INT1 (P0.03, from the locator_tag sample's board overlay). It
 * does two jobs:
 * - In READY, its interrupt marks "the skate moved", for the idle timeout.
 * - In System OFF, INT1 is a GPIO SENSE wake-up source.
 *
 * Zephyr's ADXL367 driver is disabled. Its activity mode doesn't do wake-up
 * mode, and the locator_tag sample notes its activity detection is unreliable,
 * so the few registers needed are written directly.
 */
#include "motion.h"

#include <hal/nrf_gpio.h>
#include <zephyr/drivers/gpio.h>
#include <zephyr/drivers/i2c.h>
#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>

LOG_MODULE_REGISTER(motion, LOG_LEVEL_INF);

#define REG_DEVID_AD      0x00
#define REG_PARTID        0x02
#define REG_STATUS        0x0B
#define REG_SOFT_RESET    0x1F
#define REG_THRESH_ACT_H  0x20
#define REG_THRESH_ACT_L  0x21
#define REG_TIME_ACT      0x22
#define REG_ACT_INACT_CTL 0x27
#define REG_INTMAP1_LOWER 0x2A
#define REG_FILTER_CTL    0x2C
#define REG_POWER_CTL     0x2D

#define DEVID_AD_VAL     0xAD
#define PARTID_VAL       0xF7
#define RESET_CODE       0x52
#define ACT_EN           BIT(0)
#define ACT_REF          BIT(1)
#define INTMAP_ACT       BIT(4)
#define POWER_CTL_MEASURE 0x02
#define POWER_CTL_WAKEUP BIT(3)
#define STATUS_ACT       BIT(4)

/* +-2 g range: 0.25 mg per LSB. */
#define THRESH_LSB (CONFIG_BLADING_MOTION_THRESHOLD_MG * 4)

static const struct i2c_dt_spec i2c = I2C_DT_SPEC_GET(DT_NODELABEL(adxl367));
static const struct gpio_dt_spec int1 = GPIO_DT_SPEC_GET(DT_NODELABEL(adxl367), int1_gpios);
static struct gpio_callback int1_cb;

#define INT1_NRF_PIN                                                                               \
	NRF_GPIO_PIN_MAP(DT_PROP(DT_GPIO_CTLR(DT_NODELABEL(adxl367), int1_gpios), port),          \
			 DT_GPIO_PIN(DT_NODELABEL(adxl367), int1_gpios))

static bool present;
static bool wake_armed;
static atomic_t last_motion_s;
static atomic_t isr_count;

static void rearm_fn(struct k_work *work);
static K_WORK_DELAYABLE_DEFINE(rearm_work, rearm_fn);

static int wr(uint8_t reg, uint8_t val)
{
	return i2c_reg_write_byte_dt(&i2c, reg, val);
}

/*
 * Re-enable referenced activity detection, which takes a new reference
 * (the current orientation), and clear the latched interrupt.
 */
static int arm(void)
{
	uint8_t status;
	int err = 0;

	err |= wr(REG_ACT_INACT_CTL, 0);
	err |= wr(REG_ACT_INACT_CTL, ACT_EN | ACT_REF);
	err |= i2c_reg_read_byte_dt(&i2c, REG_STATUS, &status);
	return err;
}

static void rearm_fn(struct k_work *work)
{
	arm();
}

static void int1_isr(const struct device *port, struct gpio_callback *cb, uint32_t pins)
{
	atomic_set(&last_motion_s, (atomic_val_t)k_uptime_seconds());
	atomic_inc(&isr_count);
	/* Rate-limit: re-arm (and so allow the next interrupt) a second later. */
	k_work_reschedule(&rearm_work, K_SECONDS(1));
}

int motion_init(void)
{
	uint8_t id[3];
	int err;

	atomic_set(&last_motion_s, (atomic_val_t)k_uptime_seconds());

	if (!i2c_is_ready_dt(&i2c) || !gpio_is_ready_dt(&int1)) {
		return -ENODEV;
	}

	err = wr(REG_SOFT_RESET, RESET_CODE);
	k_msleep(10);
	err |= i2c_burst_read_dt(&i2c, REG_DEVID_AD, id, sizeof(id));
	if (err || id[0] != DEVID_AD_VAL || id[2] != PARTID_VAL) {
		LOG_ERR("ADXL367 not found (err %d, id %02x/%02x)", err, id[0], id[2]);
		return -EIO;
	}

	/* Configure in standby, then start measuring in wake-up mode. */
	err |= wr(REG_FILTER_CTL, 0x00); /* +-2 g */
	err |= wr(REG_THRESH_ACT_H, (THRESH_LSB >> 6) & 0x7F);
	err |= wr(REG_THRESH_ACT_L, (THRESH_LSB & 0x3F) << 2);
	err |= wr(REG_TIME_ACT, 1);
	err |= wr(REG_INTMAP1_LOWER, INTMAP_ACT); /* active high */
	err |= wr(REG_POWER_CTL, POWER_CTL_WAKEUP | POWER_CTL_MEASURE);
	k_msleep(20);
	err |= arm();
	if (err) {
		LOG_ERR("ADXL367 setup failed");
		return -EIO;
	}

	gpio_pin_configure_dt(&int1, GPIO_INPUT);
	gpio_init_callback(&int1_cb, int1_isr, BIT(int1.pin));
	gpio_add_callback_dt(&int1, &int1_cb);
	gpio_pin_interrupt_configure_dt(&int1, GPIO_INT_EDGE_TO_ACTIVE);

	present = true;
	LOG_INF("ADXL367 wake-on-motion armed (%d mg)", CONFIG_BLADING_MOTION_THRESHOLD_MG);
	return 0;
}

bool motion_available(void)
{
	return present;
}

uint32_t motion_last_s(void)
{
	return (uint32_t)atomic_get(&last_motion_s);
}

int motion_prepare_sysoff(void)
{
	uint8_t status = 0;

	if (!present) {
		return -ENODEV;
	}

	k_work_cancel_delayable(&rearm_work);
	gpio_pin_interrupt_configure_dt(&int1, GPIO_INT_DISABLE);

	/* Reference the resting orientation; wake-up mode samples at ~6 Hz. */
	arm();
	k_msleep(400);
	arm();
	k_msleep(400);

	i2c_reg_read_byte_dt(&i2c, REG_STATUS, &status);
	int level = gpio_pin_get_dt(&int1);

	LOG_INF("sleep: ADXL367 STATUS 0x%02x, INT1 %d, %u interrupts since boot", status, level,
		(unsigned int)atomic_get(&isr_count));
	if (level != 0) {
		/* Would wake us right away (or keep waking us): button only. */
		LOG_WRN("INT1 still active, not using motion wake-up");
		return -EBUSY;
	}
	wake_armed = true;
	return 0;
}

void motion_arm_wake(void)
{
	if (wake_armed) {
		nrf_gpio_cfg_sense_set(INT1_NRF_PIN, NRF_GPIO_PIN_SENSE_HIGH);
	}
}
