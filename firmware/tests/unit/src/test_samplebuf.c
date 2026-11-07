/*
 * Sample buffer: packet boundaries, indices, timestamps and gaps.
 */
#include <zephyr/ztest.h>

#include "samplebuf.h"

#define CAP     32
#define ANCHORS 8
#define PERIOD_NS 5000000 /* 200 Hz */

static struct imu6 storage[CAP];
static struct sb_anchor anchors[ANCHORS];
static struct samplebuf sb;
static struct imu6 out[PROTO_MAX_SAMPLES_PER_PACKET];

/* Sample k carries its sample index in ax, so data can be checked. */
static void make(struct imu6 *s, uint32_t n, uint32_t first_index)
{
	for (uint32_t i = 0; i < n; i++) {
		s[i] = (struct imu6){.ax = (int16_t)(first_index + i), .az = 2048};
	}
}

static uint32_t push(uint32_t n, uint32_t index, uint64_t t_us)
{
	struct imu6 s[64];

	make(s, n, index);
	return sb_push(&sb, s, n, index, t_us);
}

static void check_out(uint32_t n, uint32_t first_index)
{
	for (uint32_t i = 0; i < n; i++) {
		zassert_equal(out[i].ax, (int16_t)(first_index + i), "sample %u", i);
	}
}

static void before(void *f)
{
	sb_init(&sb, storage, CAP, anchors, ANCHORS);
	sb_set_period_ns(&sb, PERIOD_NS);
}

ZTEST_SUITE(samplebuf, NULL, NULL, before, NULL, NULL);

ZTEST(samplebuf, test_full_packets_then_flush)
{
	uint32_t idx;
	uint64_t t;

	zassert_equal(push(20, 0, 1000000), 20);
	zassert_equal(sb_peek(&sb, out, 19, false, &idx, &t), 19);
	zassert_equal(idx, 0);
	zassert_equal(t, 1000000);
	check_out(19, 0);
	sb_consume(&sb, 19);

	/* One sample left: not a full packet yet. */
	zassert_equal(sb_peek(&sb, out, 19, false, &idx, &t), 0);
	zassert_equal(push(10, 20, 1100000), 10);
	zassert_equal(sb_peek(&sb, out, 19, false, &idx, &t), 0);

	/* Flush sends the short packet; its timestamp comes from the first anchor. */
	zassert_equal(sb_peek(&sb, out, 19, true, &idx, &t), 11);
	zassert_equal(idx, 19);
	zassert_equal(t, 1000000 + 19 * 5000);
	check_out(11, 19);
	sb_consume(&sb, 11);
	zassert_equal(sb_count(&sb), 0);
	zassert_equal(sb_peek(&sb, out, 19, true, &idx, &t), 0);
}

ZTEST(samplebuf, test_timestamp_from_nearest_anchor)
{
	uint32_t idx;
	uint64_t t;

	push(10, 0, 500000);
	/* The second batch's own (slightly later than nominal) time wins. */
	push(10, 10, 550100);
	sb_peek(&sb, out, 5, false, &idx, &t);
	sb_consume(&sb, 12);
	zassert_equal(sb_peek(&sb, out, 5, false, &idx, &t), 5);
	zassert_equal(idx, 12);
	zassert_equal(t, 550100 + 2 * 5000);
}

ZTEST(samplebuf, test_overflow_drops_new_samples_and_leaves_a_gap)
{
	uint32_t idx;
	uint64_t t;

	zassert_equal(push(20, 0, 0), 20);
	/* Only 12 fit; the newest 8 are dropped. */
	zassert_equal(push(20, 20, 100000), 12);
	zassert_equal(push(10, 40, 200000), 0);
	zassert_equal(sb_count(&sb), CAP);

	zassert_equal(sb_peek(&sb, out, 19, false, &idx, &t), 19);
	sb_consume(&sb, 19);

	/* Space again; this batch starts after the gap (indices 32..49 lost). */
	zassert_equal(push(10, 50, 250000), 10);

	/* The run before the gap goes out short, without a flush. */
	zassert_equal(sb_peek(&sb, out, 19, false, &idx, &t), 13);
	zassert_equal(idx, 19);
	check_out(13, 19);
	sb_consume(&sb, 13);

	/* The packet after the gap starts at the new index and time. */
	zassert_equal(sb_peek(&sb, out, 19, false, &idx, &t), 0);
	zassert_equal(sb_peek(&sb, out, 19, true, &idx, &t), 10);
	zassert_equal(idx, 50);
	zassert_equal(t, 250000);
	check_out(10, 50);
}

ZTEST(samplebuf, test_wraparound)
{
	uint32_t idx;
	uint64_t t;
	uint32_t next = 0;
	uint32_t sent = 0;

	/* Push and drain ~10x the capacity in uneven batches. */
	for (int round = 0; round < 40; round++) {
		uint32_t n = 3 + (round * 7) % 11;

		zassert_equal(push(n, next, next * 5000ULL), n);
		next += n;
		while (sb_peek(&sb, out, 19, false, &idx, &t) > 0) {
			zassert_equal(idx, sent);
			zassert_equal(t, sent * 5000ULL);
			check_out(19, sent);
			sb_consume(&sb, 19);
			sent += 19;
		}
	}
	uint32_t n = sb_peek(&sb, out, 19, true, &idx, &t);

	zassert_equal(idx, sent);
	check_out(n, sent);
	zassert_equal(sent + n, next);
}

ZTEST(samplebuf, test_out_of_anchors)
{
	uint32_t idx;
	uint64_t t;

	/* Contiguous batches still fit when the anchor ring is full... */
	for (uint32_t i = 0; i < ANCHORS + 2; i++) {
		zassert_equal(push(2, i * 2, i * 10000ULL), 2);
	}
	/* ...but one after a gap needs an anchor of its own. */
	zassert_equal(push(2, 100, 999999), 0);

	zassert_equal(sb_peek(&sb, out, 19, false, &idx, &t), 19);
	zassert_equal(idx, 0);
	check_out(19, 0);
}

ZTEST(samplebuf, test_reset)
{
	uint32_t idx;
	uint64_t t;

	push(20, 0, 0);
	sb_reset(&sb);
	zassert_equal(sb_count(&sb), 0);
	zassert_equal(sb_peek(&sb, out, 19, true, &idx, &t), 0);
	push(5, 0, 7);
	zassert_equal(sb_peek(&sb, out, 19, true, &idx, &t), 5);
	zassert_equal(t, 7);
}
