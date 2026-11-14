package app.bladingtag

import kotlin.math.abs

/** No Android dependencies, so accidental-touch rules can be checked in unit tests. */
internal class HoldSlideGesture(private val slop: Float) {
    var active = false
        private set
    var ready = false
        private set
    var progress = 0f
        private set
    var holdProgress = 0f
        private set
    private var startX = 0f
    private var startY = 0f
    private var startedAt = 0L
    private var slideX = 0f

    fun start(x: Float, y: Float, now: Long) {
        reset()
        active = true
        startX = x
        startY = y
        slideX = x
        startedAt = now
    }

    fun advance(now: Long) {
        if (!active) return
        if (now - startedAt > 4000) {
            reset()
            return
        }
        if (ready) return
        holdProgress = ((now - startedAt) / 1000f).coerceIn(0f, 1f)
        ready = holdProgress >= 1f
    }

    fun move(x: Float, y: Float, now: Long, travel: Float): Boolean {
        if (!active) return false
        // Moving during the hold cancels rather than converting a slow swipe into a hold.
        if ((!ready && (abs(x - startX) > slop || abs(y - startY) > slop)) ||
            abs(y - startY) > slop * 3) {
            reset()
            return false
        }
        advance(now)
        if (!active || !ready || travel <= slop * 4) return false
        progress = ((x - slideX) / travel).coerceIn(0f, 1f)
        if (progress < 0.95f) return false
        reset()
        return true
    }

    fun reset() {
        active = false
        ready = false
        progress = 0f
        holdProgress = 0f
    }
}
