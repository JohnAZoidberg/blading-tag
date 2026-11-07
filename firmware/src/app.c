#include "app.h"

#include <app_version.h>
#include <hal/nrf_gpio.h>
#include <hal/nrf_grtc.h>
#include <hal/nrf_power.h>
#include <helpers/nrfx_reset_reason.h>
#include <string.h>
#include <zephyr/drivers/gpio.h>
#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>
#include <zephyr/logging/log_ctrl.h>
#include <zephyr/settings/settings.h>
#include <zephyr/storage/flash_map.h>
#include <zephyr/sys/poweroff.h>

#include "battery.h"
#include "ble.h"
#include "imu.h"
#include "led.h"
#include "motion.h"
#include "samplebuf.h"

LOG_MODULE_REGISTER(app, LOG_LEVEL_INF);

#define BUF_SAMPLES CONFIG_BLADING_BUFFER_SAMPLES
/* One anchor per IMU batch (~100 ms); room for the whole buffer and then some. */
#define BUF_ANCHORS 1024

#define BATTERY_PERIOD   K_SECONDS(60)
#define IDLE_CHECK       K_SECONDS(30)
#define LONG_PRESS_MS    2000

/* --- State ------------------------------------------------------------------ */

/* Protects `app`. Lock order: app_lock, then sb_lock. */
static K_MUTEX_DEFINE(app_lock);
static struct {
	uint8_t state;
	/* STOP accepted; flushing the buffer before going to READY. */
	bool stopping;
	bool connected;
	bool imu_present;
	bool imu_fault;
	uint8_t position;
	uint16_t session_id;
	struct proto_rec_cfg cfg;
	uint32_t last_active_s;
} app = {
	.cfg = {PROTO_DEFAULT_ODR_HZ, PROTO_DEFAULT_ACCEL_RANGE_G, PROTO_DEFAULT_GYRO_RANGE_DPS},
};

/* Protects the sample buffer and the per-session counters. */
static K_MUTEX_DEFINE(sb_lock);
static struct imu6 sb_samples[BUF_SAMPLES];
static struct sb_anchor sb_anchors[BUF_ANCHORS];
static struct samplebuf sb;
static uint32_t samples_recorded;
static uint32_t samples_dropped;
static uint16_t seq;
/* Bumped when the buffer is reset, so the TX thread doesn't consume stale samples. */
static uint32_t sb_gen;

static K_SEM_DEFINE(tx_sem, 0, 1);
static atomic_t tx_flags;
#define TXF_STATUS 0 /* notify Status */
#define TXF_FLUSH  1 /* send everything, then finish the STOP */

static void status_work_fn(struct k_work *work);
static void link_loss_fn(struct k_work *work);
static void battery_work_fn(struct k_work *work);
static void idle_work_fn(struct k_work *work);
static void sleep_work_fn(struct k_work *work);
static K_WORK_DELAYABLE_DEFINE(status_work, status_work_fn);
static K_WORK_DELAYABLE_DEFINE(link_loss_work, link_loss_fn);
static K_WORK_DELAYABLE_DEFINE(battery_work, battery_work_fn);
static K_WORK_DELAYABLE_DEFINE(idle_work, idle_work_fn);
static K_WORK_DELAYABLE_DEFINE(sleep_work, sleep_work_fn);

static void touch_active(void)
{
	app.last_active_s = k_uptime_seconds();
}

void app_kick(void)
{
	k_sem_give(&tx_sem);
}

static void request_status(void)
{
	atomic_set_bit(&tx_flags, TXF_STATUS);
	app_kick();
}

/* --- Persistence -------------------------------------------------------------- */

static int settings_set(const char *key, size_t len, settings_read_cb read_cb, void *cb_arg)
{
	if (!strcmp(key, "sid") && len == sizeof(app.session_id)) {
		return read_cb(cb_arg, &app.session_id, len) == len ? 0 : -EIO;
	}
	if (!strcmp(key, "pos") && len == sizeof(app.position)) {
		return read_cb(cb_arg, &app.position, len) == len ? 0 : -EIO;
	}
	return 0;
}

SETTINGS_STATIC_HANDLER_DEFINE(blading, "blading", NULL, settings_set, NULL, NULL);

static void settings_init(void)
{
	int err = settings_subsys_init();

	if (err) {
		/* E.g. leftovers from the factory firmware that don't mount. */
		const struct flash_area *fa;

		LOG_WRN("settings init failed (%d), erasing storage", err);
		if (flash_area_open(PARTITION_ID(storage_partition), &fa) == 0) {
			flash_area_flatten(fa, 0, fa->fa_size);
			flash_area_close(fa);
		}
		err = settings_subsys_init();
	}
	if (err) {
		LOG_ERR("settings unavailable: %d", err);
		return;
	}
	settings_load_subtree("blading");
	if (app.position > PROTO_POS_RIGHT) {
		app.position = PROTO_POS_UNKNOWN;
	}
	LOG_INF("session_id %u, position %u", app.session_id, app.position);
}

/* --- Characteristics ------------------------------------------------------------ */

void app_encode_info(uint8_t out[PROTO_INFO_LEN])
{
	struct proto_info info = {
		.fw_major = APP_VERSION_MAJOR,
		.fw_minor = APP_VERSION_MINOR,
		.fw_patch = APP_PATCHLEVEL,
		.buffer_capacity = BUF_SAMPLES,
	};

	proto_encode_info(out, &info);
}

void app_encode_status(uint8_t out[PROTO_STATUS_LEN])
{
	struct proto_status st = {0};

	k_mutex_lock(&app_lock, K_FOREVER);
	st.state = app.state;
	st.session_id = app.session_id;
	st.cfg = app.cfg;
	st.position = app.position;
	if (app.imu_fault) {
		st.flags |= PROTO_FLAG_IMU_FAULT;
	}

	k_mutex_lock(&sb_lock, K_FOREVER);
	st.samples_recorded = samples_recorded;
	st.samples_dropped = samples_dropped;
	st.buffer_fill = sb_count(&sb);
	k_mutex_unlock(&sb_lock);
	k_mutex_unlock(&app_lock);

	if (st.samples_dropped) {
		st.flags |= PROTO_FLAG_OVERFLOW;
	}
	st.battery_mv = battery_mv();
	if (st.battery_mv && st.battery_mv < CONFIG_BLADING_LOW_BATTERY_MV) {
		st.flags |= PROTO_FLAG_LOW_BATTERY;
	}
	st.tag_time_us = tag_time_us();

	proto_encode_status(out, &st);
}

void app_adv_fields(uint8_t *state, uint8_t *position, uint16_t *battery, bool *fast)
{
	k_mutex_lock(&app_lock, K_FOREVER);
	*state = app.state;
	*position = app.position;
	*fast = app.state == PROTO_STATE_RECORDING;
	k_mutex_unlock(&app_lock);
	*battery = battery_mv();
}

/* --- State changes -------------------------------------------------------------- */

/* Call with app_lock held, after app.state or app.connected changed. */
static void state_changed_locked(void)
{
	enum led_mode mode = LED_MODE_OFF;

	if (app.state == PROTO_STATE_RECORDING) {
		mode = app.connected ? LED_MODE_RECORDING : LED_MODE_BUFFERING;
	}
	led_set_mode(mode);
	ble_adv_refresh();
	if (app.connected) {
		request_status();
		k_work_reschedule(&status_work, app.state == PROTO_STATE_RECORDING
							? K_SECONDS(1) : K_SECONDS(10));
	}
}

static void reset_buffer(void)
{
	k_mutex_lock(&sb_lock, K_FOREVER);
	sb_reset(&sb);
	sb_gen++;
	k_mutex_unlock(&sb_lock);
}

static void imu_batch(const struct imu6 *s, uint32_t n, uint32_t index, uint64_t t_us,
		      uint32_t lost)
{
	k_mutex_lock(&sb_lock, K_FOREVER);
	sb_set_period_ns(&sb, imu_period_ns());

	uint32_t stored = sb_push(&sb, s, n, index, t_us);

	samples_recorded += n + lost;
	samples_dropped += (n - stored) + lost;
	k_mutex_unlock(&sb_lock);

	app_kick();
}

static int start_recording(const struct proto_rec_cfg *cfg, bool need_link)
{
	int ret = 0;

	k_mutex_lock(&app_lock, K_FOREVER);

	if (app.state != PROTO_STATE_READY || app.stopping) {
		ret = PROTO_ERR_BAD_STATE;
		goto out;
	}
	if (need_link && (!ble_data_subscribed() || ble_mtu() < PROTO_MIN_MTU)) {
		ret = PROTO_ERR_BAD_STATE;
		goto out;
	}
	if (!app.imu_present) {
		ret = PROTO_ERR_SENSOR_FAULT;
		goto out;
	}

	k_mutex_lock(&sb_lock, K_FOREVER);
	sb_reset(&sb);
	sb_gen++;
	samples_recorded = 0;
	samples_dropped = 0;
	seq = 0;
	k_mutex_unlock(&sb_lock);
	app.cfg = *cfg;

	/*
	 * Holding app_lock until the state is RECORDING keeps the TX thread
	 * from sending Data before the RECORDING Status.
	 */
	if (imu_start(cfg) != 0) {
		app.imu_fault = true;
		request_status();
		ret = PROTO_ERR_SENSOR_FAULT;
		goto out;
	}
	app.imu_fault = false;

	app.session_id++;
	settings_save_one("blading/sid", &app.session_id, sizeof(app.session_id));

	app.state = PROTO_STATE_RECORDING;
	if (!app.connected) {
		k_work_reschedule(&link_loss_work, K_SECONDS(CONFIG_BLADING_LINK_LOSS_STOP_S));
	}
	state_changed_locked();
	LOG_INF("session %u: recording", app.session_id);
out:
	k_mutex_unlock(&app_lock);
	return ret;
}

static void stop_recording(void)
{
	k_mutex_lock(&app_lock, K_FOREVER);

	if (app.state != PROTO_STATE_RECORDING || app.stopping) {
		k_mutex_unlock(&app_lock);
		return;
	}

	/* Puts the last FIFO samples into the buffer before it returns. */
	imu_stop();
	k_work_cancel_delayable(&link_loss_work);

	if (app.connected) {
		/* The TX thread sends what's left, then the READY Status. */
		app.stopping = true;
		atomic_set_bit(&tx_flags, TXF_FLUSH);
		app_kick();
	} else {
		app.state = PROTO_STATE_READY;
		reset_buffer();
		state_changed_locked();
	}
	LOG_INF("session %u: stopped", app.session_id);
	k_mutex_unlock(&app_lock);
}

/* TX thread: all buffered data has gone out (or can't), finish the STOP. */
static void finish_stop(void)
{
	k_mutex_lock(&app_lock, K_FOREVER);
	atomic_clear_bit(&tx_flags, TXF_FLUSH);
	if (app.stopping) {
		app.stopping = false;
		app.state = PROTO_STATE_READY;
		reset_buffer();
		state_changed_locked();
	}
	k_mutex_unlock(&app_lock);
}

static void link_loss_fn(struct k_work *work)
{
	k_mutex_lock(&app_lock, K_FOREVER);
	if (app.state == PROTO_STATE_RECORDING && !app.connected && !app.stopping) {
		LOG_INF("no link for %d s, stopping", CONFIG_BLADING_LINK_LOSS_STOP_S);
		imu_stop();
		app.state = PROTO_STATE_READY;
		reset_buffer();
		touch_active();
		state_changed_locked();
	}
	k_mutex_unlock(&app_lock);
}

/* --- TX thread -------------------------------------------------------------------- */

static void send_status(void)
{
	uint8_t buf[PROTO_STATUS_LEN];

	app_encode_status(buf);
	ble_notify_status(buf, sizeof(buf), K_SECONDS(1));
}

/* Returns 1 if a packet was sent, 0 if there's nothing to send, or <0. */
static int send_data(bool flush)
{
	static struct imu6 tmp[PROTO_MAX_SAMPLES_PER_PACKET];
	static uint8_t pkt[PROTO_MAX_DATA_LEN];
	uint8_t max = proto_samples_per_packet(ble_mtu());
	uint32_t index, gen;
	uint64_t t_us;
	uint16_t s;
	int err;

	if (max == 0) {
		return -ENOTCONN;
	}
	if (!ble_data_subscribed()) {
		return -EACCES;
	}

	k_mutex_lock(&sb_lock, K_FOREVER);
	uint32_t n = sb_peek(&sb, tmp, max, flush, &index, &t_us);

	s = seq;
	gen = sb_gen;
	k_mutex_unlock(&sb_lock);

	if (n == 0) {
		return 0;
	}

	size_t len = proto_encode_data(pkt, s, index, t_us, tmp, n);

	err = ble_notify_data(pkt, len, K_SECONDS(1));
	if (err) {
		return err;
	}

	k_mutex_lock(&sb_lock, K_FOREVER);
	if (gen == sb_gen) {
		sb_consume(&sb, n);
		seq++;
	}
	k_mutex_unlock(&sb_lock);
	return 1;
}

static void tx_thread(void *a, void *b, void *c)
{
	for (;;) {
		k_sem_take(&tx_sem, K_FOREVER);

		for (;;) {
			if (atomic_test_and_clear_bit(&tx_flags, TXF_STATUS)) {
				send_status();
			}

			/* Waits for start_recording() to finish (see there). */
			k_mutex_lock(&app_lock, K_FOREVER);
			bool active = app.state == PROTO_STATE_RECORDING;
			k_mutex_unlock(&app_lock);

			if (!active) {
				break;
			}

			bool flush = atomic_test_bit(&tx_flags, TXF_FLUSH);
			int r = send_data(flush);

			if (r == 1) {
				continue;
			}
			if (r == -EAGAIN) {
				/* No TX buffer yet; the link may be congested. */
				k_msleep(10);
				continue;
			}
			if (flush) {
				/* All sent, or the link/subscription is gone: done. */
				finish_stop();
				continue;
			}
			break;
		}
	}
}

K_THREAD_DEFINE(tx_tid, 2048, tx_thread, NULL, NULL, NULL, 5, 0, 0);

/* --- Connection events ------------------------------------------------------------- */

void app_on_connected(void)
{
	k_mutex_lock(&app_lock, K_FOREVER);
	app.connected = true;
	k_work_cancel_delayable(&link_loss_work);
	state_changed_locked();
	k_mutex_unlock(&app_lock);
}

void app_on_disconnected(void)
{
	k_mutex_lock(&app_lock, K_FOREVER);
	app.connected = false;
	touch_active();
	k_work_cancel_delayable(&status_work);
	if (app.state == PROTO_STATE_RECORDING && !app.stopping) {
		k_work_reschedule(&link_loss_work, K_SECONDS(CONFIG_BLADING_LINK_LOSS_STOP_S));
	}
	state_changed_locked();
	k_mutex_unlock(&app_lock);
	/* A pending flush finishes (and drops the rest) in the TX thread. */
	app_kick();
}

static void status_work_fn(struct k_work *work)
{
	k_mutex_lock(&app_lock, K_FOREVER);
	if (app.connected) {
		request_status();
		k_work_reschedule(&status_work, app.state == PROTO_STATE_RECORDING
							? K_SECONDS(1) : K_SECONDS(10));
	}
	k_mutex_unlock(&app_lock);
}

/* --- Control ------------------------------------------------------------------------ */

int app_control(const uint8_t *buf, size_t len)
{
	struct proto_cmd cmd;
	int err = proto_parse_control(buf, len, &cmd);

	if (err) {
		return err;
	}

	switch (cmd.op) {
	case PROTO_OP_START:
		return start_recording(&cmd.start, true);
	case PROTO_OP_STOP:
		stop_recording();
		return 0;
	case PROTO_OP_IDENTIFY:
		led_identify(cmd.identify_s);
		return 0;
	case PROTO_OP_SET_POSITION:
		k_mutex_lock(&app_lock, K_FOREVER);
		if (app.state != PROTO_STATE_READY || app.stopping) {
			err = PROTO_ERR_BAD_STATE;
		} else if (app.position != cmd.position) {
			app.position = cmd.position;
			settings_save_one("blading/pos", &app.position, sizeof(app.position));
			state_changed_locked();
		}
		k_mutex_unlock(&app_lock);
		return err;
	case PROTO_OP_SLEEP:
		k_mutex_lock(&app_lock, K_FOREVER);
		if (app.state != PROTO_STATE_READY || app.stopping) {
			err = PROTO_ERR_BAD_STATE;
		} else {
			/* Let the write response go out first. */
			k_work_reschedule(&sleep_work, K_MSEC(300));
		}
		k_mutex_unlock(&app_lock);
		return err;
	default:
		return PROTO_ERR_UNKNOWN_OPCODE;
	}
}

/* --- Sleep (System OFF) ---------------------------------------------------------------- */

static const struct gpio_dt_spec button = GPIO_DT_SPEC_GET(DT_ALIAS(sw0), gpios);

/*
 * GPREGRET[1] (retained in System OFF) records how far the last sleep got,
 * so the next boot can tell a real wake-up from System OFF from a reset.
 */
#define SLEEP_MARK_REG     1
#define SLEEP_MARK_STARTED 0x5A
#define SLEEP_MARK_OFF     0xA5
/* The button is active low (pull-up). */
#define BUTTON_NRF_PIN                                                                             \
	NRF_GPIO_PIN_MAP(DT_PROP(DT_GPIO_CTLR(DT_ALIAS(sw0), gpios), port),                       \
			 DT_GPIO_PIN(DT_ALIAS(sw0), gpios))

static void sleep_work_fn(struct k_work *work)
{
	LOG_INF("going to sleep");
	nrf_power_gpregret_set(NRF_POWER, SLEEP_MARK_REG, SLEEP_MARK_STARTED);

	k_mutex_lock(&app_lock, K_FOREVER);
	if (app.state == PROTO_STATE_RECORDING) {
		imu_stop();
		app.state = PROTO_STATE_READY;
		app.stopping = false;
		reset_buffer();
	}
	k_mutex_unlock(&app_lock);

	ble_shutdown();

	led_set_mode(LED_MODE_OFF);
	for (int i = 0; i < 3; i++) {
		led_flash(LED_RED, 80);
		k_msleep(250);
	}

	if (motion_prepare_sysoff() != 0) {
		LOG_WRN("no motion wake-up, only the button wakes the tag");
	}
	gpio_pin_interrupt_configure_dt(&button, GPIO_INT_DISABLE);

	LOG_INF("powering off");
	LOG_PANIC();

	/* PRIMASK, not irq_lock(): MPSL's zero-latency IRQs must not re-arm the GRTC. */
	__disable_irq();

	/*
	 * Wake-up sources: GPIO SENSE on the button and the ADXL367 INT1. Set
	 * here, with interrupts locked, rather than as level interrupts: a line
	 * that is already active would otherwise interrupt endlessly. If one is
	 * active, System OFF is left again right away, which is what we want.
	 */
	nrf_gpio_cfg_sense_set(BUTTON_NRF_PIN, NRF_GPIO_PIN_SENSE_LOW);
	motion_arm_wake();

	/*
	 * The GRTC keeps counting in System OFF, and an armed compare channel
	 * (kernel timeout, Bluetooth controller) can wake the chip. Disarm all
	 * of them at the register level, as z_nrf_grtc_wakeup_prepare() does
	 * for the channels it knows about, so only GPIO wakes us.
	 */
	for (uint8_t ch = 0; ch < NRF_GRTC_SYSCOUNTER_CC_COUNT; ch++) {
		nrf_grtc_sys_counter_compare_event_disable(NRF_GRTC, ch);
		nrf_grtc_event_clear(NRF_GRTC, nrf_grtc_sys_counter_compare_event_get(ch));
	}
	/*
	 * CC writes take effect once latched into the GRTC's low-frequency
	 * domain; wait for that as z_nrf_grtc_wakeup_prepare() does (TIMEOUT
	 * LFCLK cycles + 77 us), or the old compare still wakes us.
	 */
	k_busy_wait(nrf_grtc_timeout_get(NRF_GRTC) * USEC_PER_SEC / 32768 + 100);

	/*
	 * Even with no compare armed (checked with a debugger at this point),
	 * a running GRTC still woke the tag from System OFF right away, with
	 * RESETREAS = GRTC. So stop it as well; Zephyr starts it again at boot,
	 * as after a power-on reset. NOT YET VERIFIED that this is enough.
	 */
	nrf_grtc_task_trigger(NRF_GRTC, NRF_GRTC_TASK_STOP);
	k_busy_wait(nrf_grtc_timeout_get(NRF_GRTC) * USEC_PER_SEC / 32768 + 100);

	nrf_power_gpregret_set(NRF_POWER, SLEEP_MARK_REG, SLEEP_MARK_OFF);
	sys_poweroff();
}

/*
 * Ready -> Asleep after CONFIG_BLADING_IDLE_SLEEP_S without motion or a
 * connection. Only with the ADXL367 working, since motion is what wakes it.
 */
static void idle_work_fn(struct k_work *work)
{
	bool sleep = false;

	k_mutex_lock(&app_lock, K_FOREVER);
	if (CONFIG_BLADING_IDLE_SLEEP_S > 0 && motion_available() &&
	    app.state == PROTO_STATE_READY && !app.stopping && !app.connected) {
		uint32_t last = MAX(app.last_active_s, motion_last_s());

		sleep = k_uptime_seconds() - last >= CONFIG_BLADING_IDLE_SLEEP_S;
	}
	k_mutex_unlock(&app_lock);

	if (sleep) {
		LOG_INF("idle for %d s", CONFIG_BLADING_IDLE_SLEEP_S);
		k_work_reschedule(&sleep_work, K_NO_WAIT);
	} else {
		k_work_reschedule(&idle_work, IDLE_CHECK);
	}
}

/* --- Button ----------------------------------------------------------------------------- */

/*
 * Short press: start (defaults) / stop recording.
 * Hold 2 s (red flash), then release: sleep. Sleep waits for the release,
 * because a held button would wake the tag again right away.
 */
static struct gpio_callback button_cb;
static bool button_down;
static bool button_long;

static void button_long_fn(struct k_work *work)
{
	if (button_down) {
		button_long = true;
		led_flash(LED_RED, 500);
	}
}

static void button_fn(struct k_work *work);
static K_WORK_DELAYABLE_DEFINE(button_work, button_fn);
static K_WORK_DELAYABLE_DEFINE(button_long_work, button_long_fn);

static void button_fn(struct k_work *work)
{
	bool down = gpio_pin_get_dt(&button) > 0;

	if (down == button_down) {
		return;
	}
	button_down = down;

	if (down) {
		button_long = false;
		k_work_reschedule(&button_long_work, K_MSEC(LONG_PRESS_MS));
		return;
	}

	k_work_cancel_delayable(&button_long_work);
	if (button_long) {
		k_work_reschedule(&sleep_work, K_NO_WAIT);
		return;
	}
	app_button_short();
}

/* System workqueue only, like the button. */
void app_button_short(void)
{
	k_mutex_lock(&app_lock, K_FOREVER);
	bool recording = app.state == PROTO_STATE_RECORDING;

	touch_active();
	k_mutex_unlock(&app_lock);

	if (recording) {
		stop_recording();
		led_flash(LED_RED, 300);
	} else {
		static const struct proto_rec_cfg defaults = {
			PROTO_DEFAULT_ODR_HZ, PROTO_DEFAULT_ACCEL_RANGE_G,
			PROTO_DEFAULT_GYRO_RANGE_DPS,
		};

		led_flash(start_recording(&defaults, false) == 0 ? LED_GREEN : LED_RED, 300);
	}
}

static void button_isr(const struct device *port, struct gpio_callback *cb, uint32_t pins)
{
	k_work_reschedule(&button_work, K_MSEC(30));
}

static void button_init(void)
{
	if (!gpio_is_ready_dt(&button)) {
		return;
	}
	gpio_pin_configure_dt(&button, GPIO_INPUT);
	gpio_init_callback(&button_cb, button_isr, BIT(button.pin));
	gpio_add_callback_dt(&button, &button_cb);
	gpio_pin_interrupt_configure_dt(&button, GPIO_INT_EDGE_BOTH);
}

/* --- Init ------------------------------------------------------------------------------- */

static void battery_work_fn(struct k_work *work)
{
	uint16_t before = battery_mv();

	if (battery_measure() == 0) {
		int diff = (int)battery_mv() - (int)before;

		if (diff > 20 || diff < -20) {
			ble_adv_refresh();
		}
	}
	k_work_reschedule(&battery_work, BATTERY_PERIOD);
}

int app_init(void)
{
	uint32_t reas = nrfx_reset_reason_get();
	uint8_t mark = nrf_power_gpregret_get(NRF_POWER, SLEEP_MARK_REG);

	LOG_INF("reset reason 0x%08x, last sleep %s", reas,
		mark == SLEEP_MARK_OFF ? "reached System OFF" :
		mark == SLEEP_MARK_STARTED ? "started, didn't finish" : "-");
	nrf_power_gpregret_set(NRF_POWER, SLEEP_MARK_REG, 0);
	nrfx_reset_reason_clear(reas);

	sb_init(&sb, sb_samples, BUF_SAMPLES, sb_anchors, BUF_ANCHORS);

	led_init();
	led_flash(LED_GREEN, 200);
	settings_init();

	if (battery_init() == 0) {
		LOG_INF("battery %u mV", battery_mv());
	}

	app.imu_present = imu_init(imu_batch) == 0;
	app.imu_fault = !app.imu_present;

	if (motion_init() != 0) {
		LOG_WRN("no wake-on-motion");
	}

	button_init();
	touch_active();
	return 0;
}

void app_run(void)
{
	k_work_reschedule(&battery_work, BATTERY_PERIOD);
	k_work_reschedule(&idle_work, IDLE_CHECK);
	ble_adv_refresh();
}
