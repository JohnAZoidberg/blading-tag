/*
 * blading-tag firmware: canary build.
 *
 * BLE advertising + SMP (mcumgr) for OTA + LED heartbeat. Confirms the
 * running image once Bluetooth is up, so MCUboot keeps it.
 */
#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>
#include <zephyr/drivers/gpio.h>
#include <zephyr/bluetooth/bluetooth.h>
#include <zephyr/bluetooth/conn.h>
#include <zephyr/dfu/mcuboot.h>
#include <app_version.h>
#include <stdio.h>

LOG_MODULE_REGISTER(main, LOG_LEVEL_INF);

static const struct gpio_dt_spec led = GPIO_DT_SPEC_GET(DT_NODELABEL(led1_green), gpios);

static char name[sizeof("BladeTag-XXXX")];

static const struct bt_data ad[] = {
	BT_DATA_BYTES(BT_DATA_FLAGS, BT_LE_AD_GENERAL | BT_LE_AD_NO_BREDR),
	/* SMP service UUID */
	BT_DATA_BYTES(BT_DATA_UUID128_ALL,
		      0x84, 0xaa, 0x60, 0x74, 0x52, 0x8a, 0x8b, 0x86,
		      0xd3, 0x4c, 0xb7, 0x1d, 0x1d, 0xdc, 0x53, 0x8d),
};

static struct bt_data sd[] = {
	BT_DATA(BT_DATA_NAME_COMPLETE, name, 0),
};

static void adv_start(void)
{
	int err = bt_le_adv_start(BT_LE_ADV_CONN_FAST_2, ad, ARRAY_SIZE(ad), sd, ARRAY_SIZE(sd));

	if (err && err != -EALREADY) {
		LOG_ERR("adv start failed: %d", err);
	}
}

static void recycled(void)
{
	adv_start();
}

BT_CONN_CB_DEFINE(conn_cb) = {
	.recycled = recycled,
};

int main(void)
{
	int err;

	LOG_INF("blading-tag canary %s", APP_VERSION_STRING);

	gpio_pin_configure_dt(&led, GPIO_OUTPUT_INACTIVE);

	err = bt_enable(NULL);
	if (err) {
		LOG_ERR("bt_enable failed: %d", err);
		return 0;
	}

	bt_addr_le_t addrs[1];
	size_t count = 1;

	bt_id_get(addrs, &count);
	snprintf(name, sizeof(name), "BladeTag-%02X%02X", addrs[0].a.val[1], addrs[0].a.val[0]);
	sd[0].data_len = strlen(name);
	bt_set_name(name);

	adv_start();

	if (!boot_is_img_confirmed()) {
		err = boot_write_img_confirmed();
		LOG_INF("image confirmed: %d", err);
	}

	for (;;) {
		gpio_pin_set_dt(&led, 1);
		k_msleep(30);
		gpio_pin_set_dt(&led, 0);
		k_msleep(1970);
	}
}
