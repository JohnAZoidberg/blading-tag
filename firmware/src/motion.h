/*
 * ADXL367 wake-on-motion.
 */
#ifndef BLADING_MOTION_H_
#define BLADING_MOTION_H_

#include <stdbool.h>
#include <stdint.h>

int motion_init(void);
bool motion_available(void);

/* Uptime in seconds of the last motion interrupt (or boot). */
uint32_t motion_last_s(void);

/* Arm INT1 as a System OFF wake-up source. Call right before sys_poweroff(). */
int motion_prepare_sysoff(void);

#endif /* BLADING_MOTION_H_ */
