package app.bladingtag

import android.content.Context
import android.os.Bundle
import android.view.MotionEvent
import android.view.View
import android.view.ViewConfiguration
import android.view.accessibility.AccessibilityNodeInfo
import android.widget.ScrollView
import android.widget.ViewFlipper
import kotlin.math.abs

/** Two independently scrolling recording pages with horizontal swipe navigation. */
class RecordingPages(context: Context, summary: View, details: View) : ViewFlipper(context) {
    private val slop = ViewConfiguration.get(context).scaledTouchSlop
    private var startX = 0f
    private var startY = 0f
    private var gesture = Direction.UNDECIDED

    private enum class Direction { UNDECIDED, HORIZONTAL, VERTICAL }

    init {
        val padding = (20 * resources.displayMetrics.density).toInt()
        for (page in listOf(summary, details)) {
            addView(ScrollView(context).apply {
                setPadding(padding, padding, padding, padding)
                addView(page)
            })
        }
        importantForAccessibility = IMPORTANT_FOR_ACCESSIBILITY_YES
    }

    override fun dispatchTouchEvent(event: MotionEvent): Boolean {
        when (event.actionMasked) {
            MotionEvent.ACTION_DOWN -> {
                startX = event.x
                startY = event.y
                gesture = Direction.UNDECIDED
            }
            MotionEvent.ACTION_MOVE -> {
                val dx = abs(event.x - startX)
                val dy = abs(event.y - startY)
                if (gesture == Direction.UNDECIDED && maxOf(dx, dy) > slop) {
                    gesture = if (dx > dy) Direction.HORIZONTAL else Direction.VERTICAL
                    if (gesture == Direction.HORIZONTAL) {
                        val cancel = MotionEvent.obtain(event)
                        cancel.action = MotionEvent.ACTION_CANCEL
                        super.dispatchTouchEvent(cancel)
                        cancel.recycle()
                    }
                }
            }
            MotionEvent.ACTION_UP -> if (gesture == Direction.HORIZONTAL) {
                val dx = event.x - startX
                if (abs(dx) > 3 * slop) displayedChild = if (dx < 0) 1 else 0
                return true
            }
        }
        if (gesture != Direction.HORIZONTAL) super.dispatchTouchEvent(event)
        return true
    }

    override fun onInitializeAccessibilityNodeInfo(info: AccessibilityNodeInfo) {
        super.onInitializeAccessibilityNodeInfo(info)
        info.isScrollable = true
        info.addAction(if (displayedChild == 0) AccessibilityNodeInfo.AccessibilityAction.ACTION_SCROLL_FORWARD
            else AccessibilityNodeInfo.AccessibilityAction.ACTION_SCROLL_BACKWARD)
    }

    override fun performAccessibilityAction(action: Int, arguments: Bundle?): Boolean {
        when {
            action == AccessibilityNodeInfo.ACTION_SCROLL_FORWARD && displayedChild == 0 -> displayedChild = 1
            action == AccessibilityNodeInfo.ACTION_SCROLL_BACKWARD && displayedChild == 1 -> displayedChild = 0
            else -> return super.performAccessibilityAction(action, arguments)
        }
        return true
    }
}
