package app.bladingtag

import android.content.Context
import android.graphics.Typeface
import android.os.SystemClock
import android.widget.LinearLayout
import android.widget.TextView

/**
 * The glanceable top of the recording screen, for taking the phone out of
 * the pocket for a moment: push rate as the one big number, speed, distance
 * and distance per push under it, the session so far as two trend charts,
 * and each tag's last seconds of motion.
 *
 * [update] runs with the activity's 500 ms refresh; [animate] redraws the
 * motion plots at frame rate while the screen is visible.
 */
class LivePanel(context: Context) : LinearLayout(context) {

    private val density = resources.displayMetrics.density
    private fun dp(v: Int) = (v * density).toInt()
    private val colors = PlotView.palette(context)
    private val semibold = Typeface.create(Typeface.DEFAULT, 600, false)

    private val elapsed = TextView(context).apply { textSize = 20f; typeface = semibold; alpha = 0.7f }
    private val pushValue = TextView(context).apply {
        textSize = 64f; typeface = semibold; includeFontPadding = false
    }
    private val pushLabel = TextView(context).apply { textSize = 14f; alpha = 0.7f }
    private val speed = tile("km/h")
    private val distance = tile("km")
    private val perPush = tile("m per push")
    private val pushTrend = PlotView(context).apply {
        title = "Pushes/min"; minSpan = 20f; emptyText = "no repetitive motion yet"
        xLabel = ::clock
    }
    private val speedTrend = PlotView(context).apply {
        title = "Speed, km/h"; minSpan = 10f; emptyText = "no GPS speed yet"
        xLabel = ::clock
    }
    private val scopeBox = LinearLayout(context).apply { orientation = VERTICAL }
    private var scopeLinks: List<TagLink> = emptyList()
    private val scopes = ArrayList<PlotView>()
    /** Tap a motion plot to switch between gyro and accel. */
    private var showAccel = false

    init {
        orientation = VERTICAL
        addView(elapsed)
        addView(pushValue, lp(4))
        addView(pushLabel)
        addView(LinearLayout(context).apply {
            orientation = HORIZONTAL
            for (t in listOf(speed, distance, perPush)) addView(t.first.parent as LinearLayout,
                LayoutParams(0, LayoutParams.WRAP_CONTENT, 1f))
        }, lp(12))
        addView(pushTrend, LayoutParams(LayoutParams.MATCH_PARENT, dp(110)).apply { topMargin = dp(16) })
        addView(speedTrend, LayoutParams(LayoutParams.MATCH_PARENT, dp(110)).apply { topMargin = dp(8) })
        addView(scopeBox, lp(8))
    }

    private fun lp(top: Int) = LayoutParams(LayoutParams.MATCH_PARENT, LayoutParams.WRAP_CONTENT)
        .apply { topMargin = dp(top) }

    /** A stat tile: (value view, label view). */
    private fun tile(label: String): Pair<TextView, TextView> {
        val box = LinearLayout(context).apply { orientation = VERTICAL }
        val v = TextView(context).apply { textSize = 26f; typeface = semibold }
        val l = TextView(context).apply { textSize = 13f; alpha = 0.7f; text = label }
        box.addView(v); box.addView(l)
        return v to l
    }

    fun update(rec: RecorderService.Recording) {
        val tr = rec.trend
        val secs = (SystemClock.elapsedRealtimeNanos() - rec.startElapsedNs) / 1_000_000_000
        elapsed.text = "%d:%02d:%02d".format(secs / 3600, secs / 60 % 60, secs % 60) +
            if (rec.stopping) "  stopping…" else ""
        val push = tr.lastPushesPerMin
        val kmh = tr.lastSpeedKmh
        pushValue.text = if (push.isNaN()) "–" else "%.0f".format(push)
        pushLabel.text = when {
            !push.isNaN() -> "pushes/min"
            rec.links.any { it.samples > 0 } -> "pushes/min · not moving"
            else -> "pushes/min · waiting for the tag"
        }
        speed.first.text = if (kmh.isNaN()) "–" else "%.1f".format(kmh)
        distance.first.text = if (rec.gps == null) "–" else "%.2f".format(rec.distanceM / 1000)
        perPush.first.text = if (push.isNaN() || kmh.isNaN()) "–" else "%.1f".format(kmh / 3.6f / (push / 60f))

        val now = (SystemClock.elapsedRealtimeNanos() - rec.startElapsedNs) / 1e9f
        // Whole session, but at least 5 minutes wide so the start isn't stretched.
        val xMax = maxOf(now, 300f)
        val t = tr.t.copyOf(tr.size)
        pushTrend.set(t, listOf(PlotView.Series("pushes/min", colors[0], tr.pushesPerMin.copyOf(tr.size))), 0f, xMax)
        speedTrend.emptyText = if (rec.gps == null) "GPS is off" else "no GPS speed yet"
        speedTrend.set(t, listOf(PlotView.Series("km/h", colors[1], tr.speedKmh.copyOf(tr.size))), 0f, xMax)

        if (rec.links != scopeLinks) {
            scopeLinks = rec.links
            scopeBox.removeAllViews()
            scopes.clear()
            for (l in rec.links) {
                val p = PlotView(context).apply {
                    fromZero = false
                    xLabel = { if (it == 0f) "now" else "%.0f s".format(it) }
                    setOnClickListener { showAccel = !showAccel; animate(rec) }
                }
                scopes.add(p)
                scopeBox.addView(p, LayoutParams(LayoutParams.MATCH_PARENT, dp(120)).apply { topMargin = dp(12) })
            }
        }
        // Repetition strength next to each tag's plot, as a hint why there's no push rate.
        for ((i, l) in rec.links.withIndex()) scopes[i].title = scopeTitle(l)
    }

    private fun scopeTitle(l: TagLink): String {
        val name = if (l.address == SimulatedTag.ADDRESS) "Simulated" else
            l.status?.let { Protocol.positionLabel(Protocol.positionName(it.position)) }
                ?.takeIf { it.isNotBlank() } ?: l.address.takeLast(5)
        val what = if (showAccel) "accel g" else "gyro °/s"
        val c = l.cadence
        val rep = if (c == null) "" else " · repetitive %.0f%%".format(100 * c.strength.coerceIn(0f, 1f))
        return "$name $what$rep"
    }

    /** Redraw the motion plots from the tags' newest samples. */
    fun animate(rec: RecorderService.Recording) {
        if (rec.links != scopeLinks) return
        for ((i, l) in rec.links.withIndex()) {
            val w = l.live.window(SCOPE_S)
            val p = scopes[i]
            p.minSpan = if (showAccel) 2f else 100f
            val base = if (showAccel) 0 else 3
            val series = listOf("x", "y", "z").mapIndexed { a, name ->
                PlotView.Series(name, colors[a], w.ch[base + a])
            }
            p.title = scopeTitle(l)
            p.set(w.t, series, -SCOPE_S, 0f)
        }
    }

    companion object {
        const val SCOPE_S = 10f

        /** Session time on the trend axis: "12 min", or "1:05 h" past an hour. */
        fun clock(s: Float): String {
            val m = (s / 60).toInt()
            return if (m < 60) "$m min" else "%d:%02d h".format(m / 60, m % 60)
        }
    }
}
