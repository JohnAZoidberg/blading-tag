/*
 * blading-tag firmware for the nRF54L15 Tag.
 *
 * Streams BMI270 accelerometer + gyroscope data over BLE when the app asks
 * for it (PROTOCOL.md), sleeps in System OFF when idle, and takes firmware
 * updates over BLE (MCUboot + mcumgr SMP). See app.c for the state machine.
 */
#include <app_version.h>
#include <zephyr/bluetooth/bluetooth.h>
#include <zephyr/dfu/mcuboot.h>
#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>

#include "app.h"
#include "ble.h"

LOG_MODULE_REGISTER(main, LOG_LEVEL_INF);

int main(void)
{
	int err;

	LOG_INF("blading-tag %s", APP_VERSION_EXTENDED_STRING);

	app_init();

	err = bt_enable(NULL);
	if (err) {
		LOG_ERR("bt_enable failed: %d", err);
		return 0;
	}

	ble_init();

	/*
	 * Bluetooth (and with it SMP) is up, so a further update is possible:
	 * keep this image. If we never get here, MCUboot reverts it on reset.
	 */
	if (!boot_is_img_confirmed()) {
		err = boot_write_img_confirmed();
		LOG_INF("image confirmed: %d", err);
	}

	app_run();
	return 0;
}
