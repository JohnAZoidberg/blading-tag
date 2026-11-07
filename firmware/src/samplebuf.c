#include "samplebuf.h"

void sb_init(struct samplebuf *sb, struct imu6 *buf, uint32_t cap, struct sb_anchor *anchors,
	     uint32_t anchor_cap)
{
	sb->buf = buf;
	sb->cap = cap;
	sb->anchors = anchors;
	sb->anchor_cap = anchor_cap;
	sb->period_ns = 5000000;
	sb_reset(sb);
}

void sb_reset(struct samplebuf *sb)
{
	sb->rpos = 0;
	sb->wpos = 0;
	sb->a_head = 0;
	sb->a_count = 0;
}

void sb_set_period_ns(struct samplebuf *sb, uint32_t period_ns)
{
	sb->period_ns = period_ns;
}

static struct sb_anchor *anchor_at(struct samplebuf *sb, uint32_t i)
{
	return &sb->anchors[(sb->a_head + i) % sb->anchor_cap];
}

/* Does anchor b continue a's index sequence without a gap? */
static bool contiguous(const struct sb_anchor *a, const struct sb_anchor *b)
{
	return b->index == a->index + (b->pos - a->pos);
}

/* Drop anchors that no stored sample needs any more. */
static void trim_anchors(struct samplebuf *sb)
{
	while (sb->a_count >= 2 && anchor_at(sb, 1)->pos <= sb->rpos) {
		sb->a_head = (sb->a_head + 1) % sb->anchor_cap;
		sb->a_count--;
	}
}

uint32_t sb_push(struct samplebuf *sb, const struct imu6 *s, uint32_t n, uint32_t index,
		 uint64_t t_us)
{
	uint32_t free = sb->cap - sb_count(sb);
	uint32_t k = n < free ? n : free;

	if (k == 0) {
		return 0;
	}

	struct sb_anchor a = {.pos = sb->wpos, .index = index, .t_us = t_us};

	trim_anchors(sb);

	if (sb->a_count == sb->anchor_cap) {
		/*
		 * Out of anchors. A batch that continues the previous one can
		 * do without (its timestamps are interpolated from an older
		 * anchor); one after a gap can't be stored.
		 */
		const struct sb_anchor *last = anchor_at(sb, sb->a_count - 1);

		if (!contiguous(last, &a)) {
			return 0;
		}
	} else {
		*anchor_at(sb, sb->a_count) = a;
		sb->a_count++;
	}

	for (uint32_t i = 0; i < k; i++) {
		sb->buf[(sb->wpos + i) % sb->cap] = s[i];
	}
	sb->wpos += k;

	return k;
}

uint32_t sb_peek(struct samplebuf *sb, struct imu6 *out, uint32_t max, bool flush,
		 uint32_t *index, uint64_t *t_us)
{
	uint32_t count = sb_count(sb);

	if (count == 0 || max == 0 || sb->a_count == 0) {
		return 0;
	}

	trim_anchors(sb);

	/* The run that can go into one packet ends at the next gap. */
	const struct sb_anchor *a = anchor_at(sb, 0);
	uint32_t run_end = sb->wpos;
	bool gap_follows = false;

	for (uint32_t i = 1; i < sb->a_count; i++) {
		const struct sb_anchor *b = anchor_at(sb, i);

		if (b->pos - sb->rpos >= max) {
			break;
		}
		if (!contiguous(a, b)) {
			run_end = b->pos;
			gap_follows = true;
			break;
		}
	}

	uint32_t avail = run_end - sb->rpos;
	uint32_t n = avail < max ? avail : max;

	if (n < max && !flush && !gap_follows) {
		return 0;
	}

	uint32_t off = sb->rpos - a->pos;

	*index = a->index + off;
	*t_us = a->t_us + ((uint64_t)off * sb->period_ns + 500) / 1000;

	for (uint32_t i = 0; i < n; i++) {
		out[i] = sb->buf[(sb->rpos + i) % sb->cap];
	}

	return n;
}

void sb_consume(struct samplebuf *sb, uint32_t n)
{
	if (n > sb_count(sb)) {
		n = sb_count(sb);
	}
	sb->rpos += n;
	trim_anchors(sb);
}
