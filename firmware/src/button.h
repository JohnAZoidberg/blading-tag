#ifndef BLADING_BUTTON_H_
#define BLADING_BUTTON_H_

#include <stdbool.h>

enum button_action { BUTTON_NONE, BUTTON_START, BUTTON_STOP, BUTTON_SLEEP };

/* Ignore presses spanning a phone-driven state change: never stop a newly
 * started recording or restart one that ended while the button was down. */
static inline enum button_action button_action(bool was_recording, bool recording, bool held)
{
	if (was_recording != recording) {
		return BUTTON_NONE;
	}
	if (recording) {
		return held ? BUTTON_STOP : BUTTON_NONE;
	}
	return held ? BUTTON_SLEEP : BUTTON_START;
}

#endif
