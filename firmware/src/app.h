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
/* Wake the TX thread (new data, notifications enabled, TX done). */
void app_kick(void);
void app_adv_fields(uint8_t *state, uint8_t *position, uint16_t *battery_mv, bool *fast);

#endif /* BLADING_APP_H_ */
