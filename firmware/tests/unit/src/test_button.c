#include <zephyr/ztest.h>
#include "button.h"

ZTEST(button, test_recording_requires_hold)
{
	zassert_equal(button_action(true, true, false), BUTTON_NONE);
	zassert_equal(button_action(true, true, true), BUTTON_STOP);
}

ZTEST(button, test_idle_start_and_sleep)
{
	zassert_equal(button_action(false, false, false), BUTTON_START);
	zassert_equal(button_action(false, false, true), BUTTON_SLEEP);
}

ZTEST(button, test_phone_changes_state_during_press)
{
	for (int held = 0; held <= 1; held++) {
		zassert_equal(button_action(false, true, held), BUTTON_NONE);
		zassert_equal(button_action(true, false, held), BUTTON_NONE);
	}
}

ZTEST_SUITE(button, NULL, NULL, NULL, NULL, NULL);
