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

/** A full-height summary and scrollable details with horizontal swipe navigation. */
class RecordingPages(context: Context, summary: View, details: View) : ViewFlipper(context) {
    var onPageChanged: (Int) -> Unit = {}
    private val slop = ViewConfiguration.get(context).scaledTouchSlop
    private var startX = 0f
    private var startY = 0f
    private var gesture = Direction.UNDECIDED

    private enum class Direction { UNDECIDED, HORIZONTAL, VERTICAL }

    init {
        val padding = (20 * resources.displayMetrics.density).toInt()
        addView(summary)
        addView(ScrollView(context).apply {
            setPadding(padding, padding, padding, padding)
            addView(details)
        })
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
                if (abs(dx) > 3 * slop) showPage(if (dx < 0) 1 else 0)
                return true
            }
        }
        if (gesture != Direction.HORIZONTAL) super.dispatchTouchEvent(event)
        return true
    }

    private fun showPage(page: Int) {
        if (displayedChild == page) return
        onPageChanged(page)
        displayedChild = page
    }

    override fun onInitializeAccessibilityNodeInfo(info: AccessibilityNodeInfo) {
        super.onInitializeAccessibilityNodeInfo(info)
        info.isScrollable = true
        info.addAction(if (displayedChild == 0) AccessibilityNodeInfo.AccessibilityAction.ACTION_SCROLL_FORWARD
            else AccessibilityNodeInfo.AccessibilityAction.ACTION_SCROLL_BACKWARD)
    }

    override fun performAccessibilityAction(action: Int, arguments: Bundle?): Boolean {
        when {
            action == AccessibilityNodeInfo.ACTION_SCROLL_FORWARD && displayedChild == 0 -> showPage(1)
            action == AccessibilityNodeInfo.ACTION_SCROLL_BACKWARD && displayedChild == 1 -> showPage(0)
            else -> return super.performAccessibilityAction(action, arguments)
        }
        return true
    }
}
