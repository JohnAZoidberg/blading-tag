#include "ble.h"

#include <stdio.h>
#include <string.h>
#include <zephyr/bluetooth/bluetooth.h>
#include <zephyr/bluetooth/conn.h>
#include <zephyr/bluetooth/gatt.h>
#include <zephyr/bluetooth/hci.h>
#include <zephyr/bluetooth/uuid.h>
#include <zephyr/logging/log.h>

#include "app.h"
#include "proto.h"

LOG_MODULE_REGISTER(ble, LOG_LEVEL_INF);

#define BLADING_UUID(short16)                                                                      \
	BT_UUID_128_ENCODE(PROTO_UUID_W32(short16), PROTO_UUID_W1, PROTO_UUID_W2, PROTO_UUID_W3,   \
			   PROTO_UUID_W48)

static const struct bt_uuid_128 svc_uuid = BT_UUID_INIT_128(BLADING_UUID(0x0001));
static const struct bt_uuid_128 info_uuid = BT_UUID_INIT_128(BLADING_UUID(0x0002));
static const struct bt_uuid_128 control_uuid = BT_UUID_INIT_128(BLADING_UUID(0x0003));
static const struct bt_uuid_128 status_uuid = BT_UUID_INIT_128(BLADING_UUID(0x0004));
static const struct bt_uuid_128 data_uuid = BT_UUID_INIT_128(BLADING_UUID(0x0005));

/* Notifications in flight at once; matches CONFIG_BT_CONN_TX_MAX. */
#define TX_CREDITS 3

static K_SEM_DEFINE(tx_credits, 0, TX_CREDITS);
static struct k_spinlock conn_lock;
static struct bt_conn *cur_conn;
/* Bumped on every connect/disconnect, so stale completions are ignored. */
static atomic_t conn_gen;
static atomic_t shutting_down;

static char name[sizeof("BladeTag-XXXX")];
static uint8_t mfg[PROTO_ADV_MFG_LEN];

static ssize_t read_info(struct bt_conn *conn, const struct bt_gatt_attr *attr, void *buf,
			 uint16_t len, uint16_t offset)
{
	uint8_t v[PROTO_INFO_LEN];

	app_encode_info(v);
	return bt_gatt_attr_read(conn, attr, buf, len, offset, v, sizeof(v));
}

static ssize_t read_status(struct bt_conn *conn, const struct bt_gatt_attr *attr, void *buf,
			   uint16_t len, uint16_t offset)
{
	uint8_t v[PROTO_STATUS_LEN];

	app_encode_status(v);
	return bt_gatt_attr_read(conn, attr, buf, len, offset, v, sizeof(v));
}

static ssize_t write_control(struct bt_conn *conn, const struct bt_gatt_attr *attr,
			     const void *buf, uint16_t len, uint16_t offset, uint8_t flags)
{
	if (offset != 0) {
		return BT_GATT_ERR(BT_ATT_ERR_INVALID_OFFSET);
	}

	int err = app_control(buf, len);

	return err ? BT_GATT_ERR(err) : len;
}

static void ccc_changed(const struct bt_gatt_attr *attr, uint16_t value)
{
	app_kick();
}

BT_GATT_SERVICE_DEFINE(blading_svc,
	BT_GATT_PRIMARY_SERVICE(&svc_uuid),
	BT_GATT_CHARACTERISTIC(&info_uuid.uuid, BT_GATT_CHRC_READ, BT_GATT_PERM_READ, read_info,
			       NULL, NULL),
	BT_GATT_CHARACTERISTIC(&control_uuid.uuid, BT_GATT_CHRC_WRITE, BT_GATT_PERM_WRITE, NULL,
			       write_control, NULL),
	BT_GATT_CHARACTERISTIC(&status_uuid.uuid, BT_GATT_CHRC_READ | BT_GATT_CHRC_NOTIFY,
			       BT_GATT_PERM_READ, read_status, NULL, NULL),
	BT_GATT_CCC(ccc_changed, BT_GATT_PERM_READ | BT_GATT_PERM_WRITE),
	BT_GATT_CHARACTERISTIC(&data_uuid.uuid, BT_GATT_CHRC_NOTIFY, BT_GATT_PERM_NONE, NULL, NULL,
			       NULL),
	BT_GATT_CCC(ccc_changed, BT_GATT_PERM_READ | BT_GATT_PERM_WRITE),
);

/* Value attributes, see the attribute list above. */
#define STATUS_ATTR (&blading_svc.attrs[6])
#define DATA_ATTR   (&blading_svc.attrs[9])

static struct bt_conn *conn_get(void)
{
	k_spinlock_key_t key = k_spin_lock(&conn_lock);
	struct bt_conn *conn = cur_conn ? bt_conn_ref(cur_conn) : NULL;

	k_spin_unlock(&conn_lock, key);
	return conn;
}

bool ble_connected(void)
{
	struct bt_conn *conn = conn_get();

	if (conn) {
		bt_conn_unref(conn);
	}
	return conn != NULL;
}

uint16_t ble_mtu(void)
{
	struct bt_conn *conn = conn_get();
	uint16_t mtu = 0;

	if (conn) {
		mtu = bt_gatt_get_mtu(conn);
		bt_conn_unref(conn);
	}
	return mtu;
}

bool ble_data_subscribed(void)
{
	struct bt_conn *conn = conn_get();
	bool on = false;

	if (conn) {
		on = bt_gatt_is_subscribed(conn, DATA_ATTR, BT_GATT_CCC_NOTIFY);
		bt_conn_unref(conn);
	}
	return on;
}

static void notify_done(struct bt_conn *conn, void *user_data)
{
	if ((atomic_val_t)(uintptr_t)user_data == atomic_get(&conn_gen)) {
		k_sem_give(&tx_credits);
	}
	app_kick();
}

static int notify(const struct bt_gatt_attr *attr, const uint8_t *buf, size_t len,
		  k_timeout_t timeout)
{
	struct bt_conn *conn = conn_get();
	int err;

	if (!conn) {
		return -ENOTCONN;
	}
	if (!bt_gatt_is_subscribed(conn, attr, BT_GATT_CCC_NOTIFY)) {
		bt_conn_unref(conn);
		return -EACCES;
	}

	atomic_val_t gen = atomic_get(&conn_gen);

	if (k_sem_take(&tx_credits, timeout) != 0) {
		bt_conn_unref(conn);
		return -EAGAIN;
	}

	struct bt_gatt_notify_params params = {
		.attr = attr,
		.data = buf,
		.len = len,
		.func = notify_done,
		.user_data = (void *)(uintptr_t)gen,
	};

	err = bt_gatt_notify_cb(conn, &params);
	if (err) {
		k_sem_give(&tx_credits);
		err = err == -ENOTCONN ? -ENOTCONN : -EAGAIN;
	}
	bt_conn_unref(conn);
	return err;
}

int ble_notify_status(const uint8_t *buf, size_t len, k_timeout_t timeout)
{
	return notify(STATUS_ATTR, buf, len, timeout);
}

int ble_notify_data(const uint8_t *buf, size_t len, k_timeout_t timeout)
{
	return notify(DATA_ATTR, buf, len, timeout);
}

/* --- Advertising ---------------------------------------------------------- */

static struct bt_data ad[] = {
	BT_DATA_BYTES(BT_DATA_FLAGS, BT_LE_AD_GENERAL | BT_LE_AD_NO_BREDR),
	BT_DATA_BYTES(BT_DATA_UUID128_ALL, BLADING_UUID(0x0001)),
	BT_DATA(BT_DATA_MANUFACTURER_DATA, mfg, sizeof(mfg)),
};

static struct bt_data sd[] = {
	BT_DATA(BT_DATA_NAME_COMPLETE, name, 0),
};

/*
 * ~1 s when idle. While recording without a link, advertise faster so the
 * phone reconnects before the buffer fills.
 */
static const struct bt_le_adv_param adv_slow =
	BT_LE_ADV_PARAM_INIT(BT_LE_ADV_OPT_CONN, 1600, 1760, NULL); /* 1.0-1.1 s */
static const struct bt_le_adv_param adv_fast =
	BT_LE_ADV_PARAM_INIT(BT_LE_ADV_OPT_CONN, 160, 240, NULL); /* 100-150 ms */

static void adv_work_fn(struct k_work *work)
{
	uint8_t state, position;
	uint16_t battery;
	bool fast;
	int err;

	if (atomic_get(&shutting_down) || ble_connected()) {
		return;
	}

	app_adv_fields(&state, &position, &battery, &fast);
	proto_encode_adv_mfg(mfg, state, position, battery);

	bt_le_adv_stop();
	err = bt_le_adv_start(fast ? &adv_fast : &adv_slow, ad, ARRAY_SIZE(ad), sd,
			      ARRAY_SIZE(sd));
	if (err && err != -EALREADY) {
		/* -ENOMEM while the old connection object isn't recycled yet. */
		LOG_DBG("adv start: %d", err);
	}
}

static K_WORK_DEFINE(adv_work, adv_work_fn);

void ble_adv_refresh(void)
{
	k_work_submit(&adv_work);
}

/* --- Connections ------------------------------------------------------------ */

static void reset_credits(void)
{
	atomic_inc(&conn_gen);
	k_sem_reset(&tx_credits);
	for (int i = 0; i < TX_CREDITS; i++) {
		k_sem_give(&tx_credits);
	}
}

static void connected(struct bt_conn *conn, uint8_t err)
{
	if (err) {
		LOG_WRN("connection failed: 0x%02x", err);
		return;
	}

	k_spinlock_key_t key = k_spin_lock(&conn_lock);

	if (cur_conn) {
		k_spin_unlock(&conn_lock, key);
		return;
	}
	cur_conn = bt_conn_ref(conn);
	k_spin_unlock(&conn_lock, key);

	reset_credits();
	LOG_INF("connected");
	app_on_connected();
}

static void disconnected(struct bt_conn *conn, uint8_t reason)
{
	k_spinlock_key_t key = k_spin_lock(&conn_lock);

	if (conn != cur_conn) {
		k_spin_unlock(&conn_lock, key);
		return;
	}
	cur_conn = NULL;
	k_spin_unlock(&conn_lock, key);
	bt_conn_unref(conn);

	reset_credits();
	LOG_INF("disconnected (0x%02x)", reason);
	app_on_disconnected();
}

/* The connection object is free again: advertising can restart. */
static void recycled(void)
{
	ble_adv_refresh();
}

BT_CONN_CB_DEFINE(conn_cb) = {
	.connected = connected,
	.disconnected = disconnected,
	.recycled = recycled,
};

int ble_init(void)
{
	bt_addr_le_t addrs[1];
	size_t count = 1;

	bt_id_get(addrs, &count);
	snprintf(name, sizeof(name), "BladeTag-%02X%02X", addrs[0].a.val[1], addrs[0].a.val[0]);
	sd[0].data_len = strlen(name);
	bt_set_name(name);

	reset_credits();
	adv_work_fn(NULL);
	LOG_INF("advertising as %s", name);
	return 0;
}

void ble_shutdown(void)
{
	atomic_set(&shutting_down, 1);

	struct bt_conn *conn = conn_get();

	if (conn) {
		bt_conn_disconnect(conn, BT_HCI_ERR_REMOTE_USER_TERM_CONN);
		bt_conn_unref(conn);
		for (int i = 0; i < 20 && ble_connected(); i++) {
			k_msleep(50);
		}
	}
	bt_le_adv_stop();
}
