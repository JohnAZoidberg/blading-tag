/*
 * BMI270 accelerometer + gyroscope, read through its FIFO.
 */
#ifndef BLADING_IMU_H_
#define BLADING_IMU_H_

#include <stdbool.h>
#include <stdint.h>

#include "proto.h"

/*
 * Called from the IMU thread for each FIFO read: n samples, the first with
 * sample index `index`, taken at t_us (tag clock). `lost` samples were lost
 * to a FIFO overflow just before this batch; `index` already skips them.
 */
typedef void (*imu_batch_cb_t)(const struct imu6 *s, uint32_t n, uint32_t index, uint64_t t_us,
			       uint32_t lost);

/* Probe the sensor. Returns 0 if it's usable. */
int imu_init(imu_batch_cb_t cb);

/*
 * Configure and start sampling. Blocks ~50 ms while the gyroscope starts up;
 * those start-up samples are discarded and sample 0 is the first valid one.
 */
int imu_start(const struct proto_rec_cfg *cfg);

/*
 * Stop sampling. Delivers the samples still in the FIFO through the callback
 * before it returns, then puts the sensor into suspend.
 */
void imu_stop(void);

/* Current estimate of the true sample period in ns (fitted to the tag clock). */
uint32_t imu_period_ns(void);

/* Tag clock: microseconds since boot (GRTC). */
uint64_t tag_time_us(void);

#endif /* BLADING_IMU_H_ */
