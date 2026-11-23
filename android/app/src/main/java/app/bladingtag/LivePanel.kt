package app.bladingtag

import android.content.Context
import android.graphics.Typeface
import android.os.SystemClock
import android.view.Gravity
import android.widget.LinearLayout
import android.widget.TableLayout
import android.widget.TableRow
import android.widget.TextView

/** Recording details, including session totals and live plots. */
class LivePanel(context: Context, private val summary: LiveStats) : LinearLayout(context) {
    private fun dp(v: Int) = (v * resources.displayMetrics.density).toInt()
    private val colors = PlotView.palette(context)
    private val stats = TableLayout(context).apply {
        setColumnStretchable(0, true)
        setColumnShrinkable(0, true)
    }
    private val peakHeartRate = stat("Highest heart rate", "bpm")
    private val peakSpeed = stat("Highest speed", "km/h")
    private val peakElevation = stat("Highest elevation", "m")
    private val ascent = stat("Ascent", "m")
    private val descent = stat("Descent", "m")
    private val distancePerCycle = stat("", "m")
    private val elevationTrend = PlotView(context).apply {
        title = "Elevation, m"; minSpan = 20f; emptyText = "waiting for elevation…"
        fitRange = true
        xLabel = ::clock
    }
    private val rateTrend = PlotView(context).apply {
        minSpan = 20f; emptyText = "no repetitive motion yet"
        xLabel = ::clock
    }
    private val speedTrend = PlotView(context).apply {
        title = "Speed, km/h"; minSpan = 10f; emptyText = "no GPS speed yet"
        xLabel = ::clock
    }
    private val heartRateTrend = PlotView(context).apply {
        title = "Heart rate, bpm"; minSpan = 100f; emptyText = "waiting for heart rate…"
        xLabel = ::clock
    }
    private val scopeBox = LinearLayout(context).apply { orientation = VERTICAL }
    private var scopeLinks: List<TagLink> = emptyList()
    private val scopes = ArrayList<PlotView>()
    /** Tap a motion plot to switch between gyro and accel. */
    private var showAccel = false

    init {
        orientation = VERTICAL
        addView(stats)
        for (plot in listOf(heartRateTrend, speedTrend, rateTrend, elevationTrend)) {
            addView(plot, LayoutParams(LayoutParams.MATCH_PARENT, dp(120)).apply { topMargin = dp(8) })
        }
        addView(scopeBox)
    }

    fun update(rec: RecorderService.Recording, detailsVisible: Boolean) {
        summary.update(rec)
        if (!detailsVisible) return

        val hasGps = rec.gps != null
        val hasHrm = rec.heartRateLink != null
        val hasTags = rec.links.isNotEmpty()
        val tr = rec.trend
        peakHeartRate.show(hasHrm, tr.peakHeartRateBpm, 0)
        peakSpeed.show(hasGps, tr.peakSpeedKmh, 1)
        peakElevation.show(hasGps, tr.elevation.peakM, 0)
        ascent.show(hasGps, tr.elevation.ascentM, 0)
        descent.show(hasGps, tr.elevation.descentM, 0)
        stats.visibility = if (hasGps || hasHrm) VISIBLE else GONE

        val unit = RateUnit.of(rec.meta.activity)
        val kmh = tr.lastSpeedKmh
        val metres = if (kmh >= MIN_PER_CYCLE_KMH) unit.distancePer(kmh, tr.lastCyclesPerMin) else Float.NaN
        distancePerCycle.label.text = "Distance per ${unit.one}"
        distancePerCycle.show(hasGps && hasTags, metres, if (unit.perCycle == 1) 2 else 1)

        val now = (SystemClock.elapsedRealtimeNanos() - rec.startElapsedNs) / 1e9f
        val end = maxOf(now, 1f)
        val start = maxOf(0f, end - 15 * 60)
        val first = tr.firstAtOrAfter(start)
        val t = tr.t.copyOfRange(first, tr.size)
        fun plot(view: PlotView, enabled: Boolean, label: String, color: Int, values: FloatArray, scale: Int = 1) {
            view.visibility = if (enabled) VISIBLE else GONE
            if (enabled) {
                val y = FloatArray(t.size) { values[first + it] * scale }
                view.set(t, listOf(PlotView.Series(label, color, y)), start, end)
            }
        }

        rateTrend.title = unit.title
        plot(rateTrend, hasTags, unit.rate, colors[0], tr.cyclesPerMin, unit.perCycle)
        plot(speedTrend, hasGps, "km/h", colors[1], tr.speedKmh)
        plot(heartRateTrend, hasHrm, "bpm", colors[2], tr.heartRateBpm)
        plot(elevationTrend, hasGps, "m", colors[0], tr.elevationM)

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
    }

    private fun stat(caption: String, unit: String) = StatRow(caption, unit).also { stats.addView(it) }

    private inner class StatRow(caption: String, unit: String) : TableRow(this@LivePanel.context) {
        val label = cell(caption).apply { setPaddingRelative(0, 0, dp(12), 0) }
        private val value = cell("").apply {
            gravity = Gravity.END or Gravity.CENTER_VERTICAL
            typeface = Typeface.create(Typeface.DEFAULT, 600, false)
            fontFeatureSettings = "tnum"
        }

        init {
            gravity = Gravity.CENTER_VERTICAL
            setPadding(0, dp(4), 0, dp(4))
            addView(label)
            addView(value)
            addView(cell(unit).apply {
                alpha = 0.75f
                setPaddingRelative(dp(8), 0, 0, 0)
            })
        }

        private fun cell(content: String) = TextView(context).apply {
            text = content
            textSize = 16f
        }

        fun show(enabled: Boolean, number: Float, decimals: Int) {
            visibility = if (enabled) VISIBLE else GONE
            if (enabled) {
                val formatted = formatLiveValue(number, decimals)
                if (value.text != formatted) value.text = formatted
            }
        }
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
        const val MIN_PER_CYCLE_KMH = 3f

        /** Elapsed recording time on the trend axis. */
        fun clock(s: Float): String {
            val seconds = s.toInt()
            return if (seconds < 3600) "%d:%02d".format(seconds / 60, seconds % 60)
                else "%d:%02d:%02d".format(seconds / 3600, seconds / 60 % 60, seconds % 60)
        }
    }
}
