package app.bladingtag

import android.content.Context
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.Paint
import android.os.SystemClock
import android.view.HapticFeedbackConstants
import android.view.MotionEvent
import android.view.View
import android.view.ViewConfiguration
import kotlin.math.abs

/** A continuous hold followed by a fresh horizontal slide; releasing always resets it. */
class HoldSlideView(context: Context, private val confirmed: () -> Unit) : View(context) {
    private val density = resources.displayMetrics.density
    private val paint = Paint(Paint.ANTI_ALIAS_FLAG)
    private val gesture = HoldSlideGesture(ViewConfiguration.get(context).scaledTouchSlop.toFloat())
    private var announcedReady = false
    private val update = object : Runnable {
        override fun run() {
            if (!gesture.active) return
            gesture.advance(SystemClock.uptimeMillis())
            if (gesture.ready && !announcedReady) {
                announcedReady = true
                performHapticFeedback(HapticFeedbackConstants.LONG_PRESS)
            }
            invalidate()
            postDelayed(this, 16)
        }
    }

    init {
        contentDescription = "Hold the left circle for one second, then slide right without lifting. Release to cancel."
        isFocusable = true
    }

    override fun onDraw(canvas: Canvas) {
        super.onDraw(canvas)
        val radius = 24 * density
        val left = radius + 8 * density
        val right = width - left
        val cy = height / 2f
        paint.color = Color.DKGRAY
        canvas.drawRoundRect(left - radius, cy - radius, right + radius, cy + radius, radius, radius, paint)
        paint.color = if (gesture.ready) Color.rgb(130, 195, 160) else Color.LTGRAY
        val x = left + gesture.progress * (right - left)
        canvas.drawCircle(x, cy, radius, paint)
        paint.color = Color.WHITE
        paint.textSize = 14 * resources.displayMetrics.scaledDensity
        paint.textAlign = Paint.Align.CENTER
        val text = if (gesture.ready) "Slide right →" else "Hold 1 second →"
        canvas.drawText(text, width / 2f + 12 * density, cy + 5 * density, paint)
        if (gesture.active && !gesture.ready) {
            paint.style = Paint.Style.STROKE
            paint.strokeWidth = 3 * density
            canvas.drawArc(left - radius, cy - radius, left + radius, cy + radius,
                -90f, 360f * gesture.holdProgress, false, paint)
            paint.style = Paint.Style.FILL
        }
    }

    override fun onTouchEvent(event: MotionEvent): Boolean {
        val left = 32 * density
        val travel = width - 2 * left
        when (event.actionMasked) {
            MotionEvent.ACTION_DOWN -> {
                if (abs(event.x - left) > 28 * density || abs(event.y - height / 2f) > 28 * density) return false
                announcedReady = false
                gesture.start(event.x, event.y, event.eventTime)
                parent.requestDisallowInterceptTouchEvent(true)
                post(update)
            }
            MotionEvent.ACTION_MOVE -> {
                if (gesture.move(event.x, event.y, event.eventTime, travel)) {
                    reset()
                    performHapticFeedback(HapticFeedbackConstants.CONFIRM)
                    confirmed()
                }
            }
            MotionEvent.ACTION_UP, MotionEvent.ACTION_CANCEL, MotionEvent.ACTION_POINTER_DOWN -> reset()
        }
        invalidate()
        return true
    }

    fun reset() {
        gesture.reset()
        removeCallbacks(update)
        parent?.requestDisallowInterceptTouchEvent(false)
        invalidate()
    }

    override fun onDetachedFromWindow() {
        reset()
        super.onDetachedFromWindow()
    }
}
