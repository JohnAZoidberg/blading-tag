/*
 * Tag state machine (PROTOCOL.md 1.4-1.9): READY / RECORDING, the sample
 * buffer and Data packets, link loss, persistence, button, sleep.
 */
#ifndef BLADING_APP_H_
#define BLADING_APP_H_

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "proto.h"

/* Before bt_enable(): settings, LED, sensors, button. */
int app_init(void);
/* After Bluetooth is up: periodic work, advertising. */
void app_run(void);

/* Called by ble.c. */
void app_encode_info(uint8_t out[PROTO_INFO_LEN]);
void app_encode_status(uint8_t out[PROTO_STATUS_LEN]);
/* Returns 0 or a PROTO_ERR_* code. */
int app_control(const uint8_t *buf, size_t len);
void app_on_connected(void);
void app_on_disconnected(void);
/* The peer acknowledged the Data packet sent with this token. */
void app_on_data_acked(uint32_t token);
/* Wake the TX thread (new data, notifications enabled, TX done). */
void app_kick(void);
void app_adv_fields(uint8_t *state, uint8_t *position, uint16_t *battery_mv, bool *fast);

/* What a short button press does: start (defaults) while idle, otherwise ignore. Also
 * used by testhook.c. Call from the system workqueue. */
void app_button_short(void);
/* Release after a press begun in was_recording; held means the 2 s threshold passed. */
void app_button_release(bool was_recording, bool held);

#endif /* BLADING_APP_H_ */
