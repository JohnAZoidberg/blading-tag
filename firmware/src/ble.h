/*
 * Bluetooth side of the protocol: advertising, the Blading GATT service and
 * notification flow control. The protocol state lives in app.c.
 */
#ifndef BLADING_BLE_H_
#define BLADING_BLE_H_

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <zephyr/kernel.h>

/* Call after bt_enable(). Sets the name and starts advertising. */
int ble_init(void);

/* Re-read the advertised state (state, position, battery) and restart. */
void ble_adv_refresh(void);

bool ble_connected(void);
/* ATT MTU of the current connection, 0 if none. */
uint16_t ble_mtu(void);
bool ble_data_subscribed(void);

/*
 * Notify Status / Data. At most a few notifications are in flight at once;
 * these wait up to `timeout` for one to complete.
 * Returns 0, -ENOTCONN, -EACCES (not subscribed) or -EAGAIN (timed out).
 *
 * Once the peer has acknowledged a Data notification, ble.c calls
 * app_on_data_acked(token), in order. It doesn't for one lost to a
 * disconnect. token has 24 bits.
 */
int ble_notify_status(const uint8_t *buf, size_t len, k_timeout_t timeout);
int ble_notify_data(const uint8_t *buf, size_t len, k_timeout_t timeout, uint32_t token);

/* Disconnect, stop advertising and keep it stopped (before System OFF). */
void ble_shutdown(void);

#endif /* BLADING_BLE_H_ */
