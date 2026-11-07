/*
 * Coin-cell voltage. The Tag runs straight off the CR2032, so VDD is the
 * battery voltage. Measured with the SAADC's internal VDD input (see
 * blading.overlay: gain 1/4, 0.9 V reference, so 3.6 V full scale).
 */
#include "battery.h"

#include <zephyr/drivers/adc.h>
#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>

LOG_MODULE_REGISTER(battery, LOG_LEVEL_INF);

static const struct adc_dt_spec vdd = ADC_DT_SPEC_GET(DT_PATH(zephyr_user));
static atomic_t last_mv;
static bool ready;

int battery_init(void)
{
	if (!adc_is_ready_dt(&vdd)) {
		return -ENODEV;
	}
	int err = adc_channel_setup_dt(&vdd);

	if (err) {
		LOG_ERR("ADC setup failed: %d", err);
		return err;
	}
	ready = true;
	return battery_measure();
}

int battery_measure(void)
{
	int16_t raw;
	struct adc_sequence seq = {
		.buffer = &raw,
		.buffer_size = sizeof(raw),
	};
	int32_t mv;
	int err;

	if (!ready) {
		return -ENODEV;
	}

	adc_sequence_init_dt(&vdd, &seq);
	err = adc_read_dt(&vdd, &seq);
	if (err) {
		return err;
	}

	mv = raw;
	err = adc_raw_to_millivolts_dt(&vdd, &mv);
	if (err) {
		return err;
	}

	atomic_set(&last_mv, CLAMP(mv, 0, UINT16_MAX));
	return 0;
}

uint16_t battery_mv(void)
{
	return (uint16_t)atomic_get(&last_mv);
}
