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
 * Samples that went out in a notification stay stored until the peer
 * acknowledges them, so they can be sent again if the link drops first:
 *
 *   [rpos, spos)  sent, waiting for the acknowledgement
 *   [spos, wpos)  not sent yet
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

	/* Monotonic positions: samples [rpos, wpos) are stored, [spos, wpos)
	 * not sent yet. rpos <= spos <= wpos.
	 */
	uint32_t rpos, spos, wpos;
	/* Anchor ring: a_count anchors starting at a_head, oldest first. */
	uint32_t a_head, a_count;
	/* Sample period for timestamp interpolation, in ns. */
	uint32_t period_ns;
};

void sb_init(struct samplebuf *sb, struct imu6 *buf, uint32_t cap, struct sb_anchor *anchors,
	     uint32_t anchor_cap);
void sb_reset(struct samplebuf *sb);
void sb_set_period_ns(struct samplebuf *sb, uint32_t period_ns);

/* Samples stored, including sent ones that aren't acknowledged yet. */
static inline uint32_t sb_count(const struct samplebuf *sb)
{
	return sb->wpos - sb->rpos;
}

/* Samples not sent yet. */
static inline uint32_t sb_unsent(const struct samplebuf *sb)
{
	return sb->wpos - sb->spos;
}

/*
 * Store a batch of n consecutive samples; the first has sample index `index`
 * and was taken at t_us. Stores as many as fit, oldest first, and returns
 * that number. The caller counts the rest as dropped.
 */
uint32_t sb_push(struct samplebuf *sb, const struct imu6 *s, uint32_t n, uint32_t index,
		 uint64_t t_us);

/*
 * Copy up to max samples for the next packet into out, starting at the first
 * unsent sample, without removing them or marking them sent.
 * Returns the number copied, and the first sample's index and timestamp.
 *
 * A short packet is only returned if `flush` is set or a gap follows it;
 * otherwise this returns 0 until max samples are available.
 */
uint32_t sb_peek(struct samplebuf *sb, struct imu6 *out, uint32_t max, bool flush,
		 uint32_t *index, uint64_t *t_us);

/* Mark the n samples sb_peek just returned as sent. */
void sb_mark_sent(struct samplebuf *sb, uint32_t n);

/* Undo the last sb_mark_sent(n), when the packet couldn't be queued. */
void sb_unmark_sent(struct samplebuf *sb, uint32_t n);

/*
 * Remove the oldest n samples (the peer acknowledged them). Also marks them
 * sent, so peek + consume without sb_mark_sent works too.
 */
void sb_consume(struct samplebuf *sb, uint32_t n);

/* The link dropped: send everything that isn't acknowledged again. */
void sb_rewind(struct samplebuf *sb);

#endif /* BLADING_SAMPLEBUF_H_ */
