package app.bladingtag

import org.junit.Assert.*
import org.junit.Test

class HoldSlideGestureTest {
    private val gesture = HoldSlideGesture(10f)

    @Test fun stationaryPressureNeverConfirms() {
        gesture.start(0f, 0f, 0)
        gesture.advance(60_000)
        assertFalse(gesture.move(0f, 0f, 60_001, 200f))
        assertFalse(gesture.active)
    }

    @Test fun immediateSwipeConfirms() {
        gesture.start(0f, 0f, 0)
        assertTrue(gesture.move(200f, 0f, 100, 200f))
        assertFalse(gesture.active)
    }

    @Test fun releaseRequiresAnotherFullSlide() {
        gesture.start(0f, 0f, 0)
        assertFalse(gesture.move(100f, 0f, 100, 200f))
        gesture.reset()
        assertFalse(gesture.move(200f, 0f, 200, 200f))
        gesture.start(0f, 0f, 300)
        assertFalse(gesture.move(100f, 0f, 400, 200f))
        assertTrue(gesture.move(200f, 0f, 500, 200f))
    }

    @Test fun partialSlideThenFullSlideConfirmsOnce() {
        gesture.start(0f, 0f, 0)
        assertFalse(gesture.move(100f, 0f, 1100, 200f))
        assertTrue(gesture.move(200f, 0f, 1200, 200f))
        assertFalse(gesture.move(200f, 0f, 1300, 200f))
    }

    @Test fun delayedSlideAfterExpiryCannotConfirm() {
        gesture.start(0f, 0f, 0)
        assertFalse(gesture.move(200f, 0f, 5000, 200f))
        assertFalse(gesture.active)
    }

    @Test fun verticalMovementCancels() {
        gesture.start(0f, 0f, 0)
        assertFalse(gesture.move(200f, 40f, 1200, 200f))
        assertFalse(gesture.active)
    }
}
