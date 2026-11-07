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

/*
 * Re-reference the ADXL367 for System OFF. Returns 0 if INT1 is idle and can
 * be a wake-up source, -EBUSY if it's still active (then only the button wakes).
 */
int motion_prepare_sysoff(void);

/*
 * Set GPIO SENSE on INT1 (if motion_prepare_sysoff() succeeded). Call with
 * interrupts locked, right before sys_poweroff(): a line that is already
 * active must not get a live level interrupt, or it fires endlessly.
 */
void motion_arm_wake(void);

#endif /* BLADING_MOTION_H_ */
