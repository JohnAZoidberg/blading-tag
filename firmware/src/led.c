#include <zephyr/drivers/gpio.h>
#include <zephyr/kernel.h>
#include <zephyr/sys/util.h>

#include "led.h"

static const struct gpio_dt_spec leds[] = {
	GPIO_DT_SPEC_GET(DT_NODELABEL(led1_red), gpios),
	GPIO_DT_SPEC_GET(DT_NODELABEL(led1_green), gpios),
	GPIO_DT_SPEC_GET(DT_NODELABEL(led1_blue), gpios),
};

#define BLIP_MS 30

static struct k_spinlock lock;
static enum led_mode mode;
static bool dropping;
static int64_t flash_until;
static uint8_t flash_colors;
static int64_t identify_until;

static void tick_fn(struct k_work *work);
static K_WORK_DELAYABLE_DEFINE(tick_work, tick_fn);

static void show(uint8_t colors)
{
	for (size_t i = 0; i < ARRAY_SIZE(leds); i++) {
		gpio_pin_set_dt(&leds[i], (colors >> i) & 1);
	}
}

static void tick_fn(struct k_work *work)
{
	int64_t now = k_uptime_get();
	uint8_t colors = 0;
	int64_t next = -1;
	k_spinlock_key_t key = k_spin_lock(&lock);

	if (now < flash_until) {
		colors = flash_colors;
		next = flash_until - now;
	} else if (now < identify_until) {
		int64_t ph = now % 500;

		colors = ph < 250 ? LED_BLUE : 0;
		next = (ph < 250 ? 250 : 500) - ph;
	} else if (mode != LED_MODE_OFF) {
		int64_t period = mode == LED_MODE_RECORDING ? 2000 : 1000;
		int64_t ph = now % period;

		if (ph < BLIP_MS) {
			colors = mode == LED_MODE_RECORDING ? LED_GREEN
				 : dropping		     ? LED_RED
							     : LED_YELLOW;
			next = BLIP_MS - ph;
		} else {
			next = period - ph;
		}
	}
	k_spin_unlock(&lock, key);

	show(colors);
	if (next >= 0) {
		k_work_reschedule(&tick_work, K_MSEC(MAX(next, 1)));
	}
}

void led_init(void)
{
	for (size_t i = 0; i < ARRAY_SIZE(leds); i++) {
		gpio_pin_configure_dt(&leds[i], GPIO_OUTPUT_INACTIVE);
	}
}

void led_set_mode(enum led_mode m)
{
	k_spinlock_key_t key = k_spin_lock(&lock);

	mode = m;
	k_spin_unlock(&lock, key);
	k_work_reschedule(&tick_work, K_NO_WAIT);
}

void led_set_dropping(bool d)
{
	k_spinlock_key_t key = k_spin_lock(&lock);
	bool changed = dropping != d;

	dropping = d;
	k_spin_unlock(&lock, key);
	if (changed) {
		k_work_reschedule(&tick_work, K_NO_WAIT);
	}
}

void led_flash(uint8_t colors, uint16_t ms)
{
	k_spinlock_key_t key = k_spin_lock(&lock);

	flash_colors = colors;
	flash_until = k_uptime_get() + ms;
	k_spin_unlock(&lock, key);
	k_work_reschedule(&tick_work, K_NO_WAIT);
}

void led_identify(uint8_t seconds)
{
	k_spinlock_key_t key = k_spin_lock(&lock);

	identify_until = seconds ? k_uptime_get() + seconds * 1000 : 0;
	k_spin_unlock(&lock, key);
	k_work_reschedule(&tick_work, K_NO_WAIT);
}
