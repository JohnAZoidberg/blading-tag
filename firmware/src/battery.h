#ifndef BLADING_BATTERY_H_
#define BLADING_BATTERY_H_

#include <stdint.h>

int battery_init(void);
/* Take a new reading. */
int battery_measure(void);
/* Last reading in mV, 0 if none. */
uint16_t battery_mv(void);

#endif /* BLADING_BATTERY_H_ */
