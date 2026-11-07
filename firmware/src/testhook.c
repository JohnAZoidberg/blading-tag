/*
 * Test builds only (CONFIG_BLADING_TEST_HOOKS, see test.conf): lets the
 * hardware tests in host/tests/hw act like a person holding the tag.
 *
 * The tests find `blading_test` in zephyr.elf and use the J-Link to write a
 * command into it while the CPU keeps running (no halt). A work item polls
 * the mailbox, runs the command on the system workqueue, the same context as
 * the real button, and clears `cmd` when done.
 */
#include <errno.h>
#include <zephyr/init.h>
#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>
#include <zephyr/sys/util.h>

#include "app.h"

LOG_MODULE_REGISTER(testhook, LOG_LEVEL_INF);

#define TESTHOOK_MAGIC 0xB1AD7E57u
#define POLL K_MSEC(20)

enum testhook_cmd {
	TESTHOOK_NONE = 0,
	/* What a short press of the button does. */
	TESTHOOK_BUTTON_SHORT = 1,
};

/* Layout is read by host/src/bladetag/jlink.py; keep them in sync. */
struct testhook_mailbox {
	/* TESTHOOK_MAGIC once polling runs: the host's check that this ELF
	 * matches the image on the tag. */
	uint32_t magic;
	/* Written by the host, cleared to TESTHOOK_NONE when done. */
	uint32_t cmd;
	/* Commands run since boot. */
	uint32_t done;
	/* Last command: 0, or -EINVAL for an unknown one. */
	int32_t result;
};

__used volatile struct testhook_mailbox blading_test;

static void poll_fn(struct k_work *work);
static K_WORK_DELAYABLE_DEFINE(poll_work, poll_fn);

static void poll_fn(struct k_work *work)
{
	uint32_t cmd = blading_test.cmd;

	blading_test.magic = TESTHOOK_MAGIC;

	if (cmd != TESTHOOK_NONE) {
		int32_t res = 0;

		LOG_INF("command %u", cmd);
		switch (cmd) {
		case TESTHOOK_BUTTON_SHORT:
			app_button_short();
			break;
		default:
			res = -EINVAL;
			break;
		}
		blading_test.result = res;
		blading_test.done++;
		blading_test.cmd = TESTHOOK_NONE;
	}
	k_work_reschedule(&poll_work, POLL);
}

static int testhook_init(void)
{
	blading_test.cmd = TESTHOOK_NONE;
	/* First poll (and the magic) after main() has run app_init() and
	 * brought Bluetooth up. */
	k_work_reschedule(&poll_work, K_SECONDS(1));
	LOG_WRN("test hooks on: test build, not for normal use");
	return 0;
}

SYS_INIT(testhook_init, APPLICATION, 99);
