/*
 * Sample ring buffer that turns IMU batches into Data packets.
 *
 * Samples are stored contiguously. Each pushed batch also records an
 * "anchor" (buffer position, sample_index, timestamp of its first sample),
 * from which a packet's sample_index and timestamp are derived:
 *
 *   index = anchor.index + (pos - anchor.pos)
 *   t_us  = anchor.t_us  + (pos - anchor.pos) * period
 *
 * When samples are dropped (buffer full, or IMU FIFO overflow) the next
 * batch's index jumps. A packet never spans such a gap, so clients see it as
 * a jump in sample_index, as PROTOCOL.md 1.7 describes.
 *
 * Not thread safe; the caller locks. Pure C so it can be unit tested.
 */
#ifndef BLADING_SAMPLEBUF_H_
#define BLADING_SAMPLEBUF_H_

#include <stdbool.h>
#include <stdint.h>

#include "proto.h"

struct sb_anchor {
	uint32_t pos;
	uint32_t index;
	uint64_t t_us;
};

struct samplebuf {
	struct imu6 *buf;
	uint32_t cap;
	struct sb_anchor *anchors;
	uint32_t anchor_cap;

	/* Monotonic positions: samples [rpos, wpos) are stored. */
	uint32_t rpos, wpos;
	/* Anchor ring: a_count anchors starting at a_head, oldest first. */
	uint32_t a_head, a_count;
	/* Sample period for timestamp interpolation, in ns. */
	uint32_t period_ns;
};

void sb_init(struct samplebuf *sb, struct imu6 *buf, uint32_t cap, struct sb_anchor *anchors,
	     uint32_t anchor_cap);
void sb_reset(struct samplebuf *sb);
void sb_set_period_ns(struct samplebuf *sb, uint32_t period_ns);

static inline uint32_t sb_count(const struct samplebuf *sb)
{
	return sb->wpos - sb->rpos;
}

/*
 * Store a batch of n consecutive samples; the first has sample index `index`
 * and was taken at t_us. Stores as many as fit, oldest first, and returns
 * that number. The caller counts the rest as dropped.
 */
uint32_t sb_push(struct samplebuf *sb, const struct imu6 *s, uint32_t n, uint32_t index,
		 uint64_t t_us);

/*
 * Copy up to max samples for the next packet into out, without removing them.
 * Returns the number copied, and the first sample's index and timestamp.
 *
 * A short packet is only returned if `flush` is set or a gap follows it;
 * otherwise this returns 0 until max samples are available.
 */
uint32_t sb_peek(struct samplebuf *sb, struct imu6 *out, uint32_t max, bool flush,
		 uint32_t *index, uint64_t *t_us);

/* Remove n samples (after sb_peek returned at least n). */
void sb_consume(struct samplebuf *sb, uint32_t n);

#endif /* BLADING_SAMPLEBUF_H_ */
