package app.bladingtag

import android.content.Context
import android.graphics.Typeface
import android.os.SystemClock
import android.widget.LinearLayout
import android.widget.TextView

/** Glanceable values for the sensors enabled in this recording. */
class LiveStats(context: Context) : LinearLayout(context) {
    private val elapsed = TextView(context).apply { textSize = 18f }
    private val heartRate = Stat("Heart rate · bpm")
    private val speed = Stat("Current speed · km/h")
    private val distance = Stat("Total distance · km")
    private val cadence = Stat("Cadence")
    private val elevation = Stat("Current elevation · m")

    init {
        orientation = VERTICAL
        addView(elapsed)
        for (stat in listOf(heartRate, speed, distance, cadence, elevation)) {
            addView(stat, LayoutParams(LayoutParams.MATCH_PARENT, LayoutParams.WRAP_CONTENT).apply {
                topMargin = (10 * resources.displayMetrics.density).toInt()
            })
        }
        addView(TextView(context).apply {
            text = "Swipe left for details and recording controls"
            textSize = 14f
            alpha = 0.75f
            setPadding(0, (16 * resources.displayMetrics.density).toInt(), 0, 0)
        })
    }

    fun update(rec: RecorderService.Recording) {
        val secs = (SystemClock.elapsedRealtimeNanos() - rec.startElapsedNs) / 1_000_000_000
        elapsed.text = "%d:%02d:%02d".format(secs / 3600, secs / 60 % 60, secs % 60) +
            if (rec.stopping) "  stopping…" else ""
        val unit = RateUnit.of(rec.meta.activity)
        val hrm = rec.heartRateLink
        heartRate.show(hrm != null, hrm?.liveBpm() ?: Float.NaN, 0)
        speed.show(rec.gps != null, rec.trend.lastSpeedKmh, 1)
        distance.show(rec.gps != null, if (rec.lastFix == null) Float.NaN else (rec.distanceM / 1000).toFloat(), 2)
        cadence.label.text = "Cadence · ${unit.rate}"
        cadence.show(rec.links.isNotEmpty(), unit.rate(rec.trend.lastCyclesPerMin), 0)
        elevation.show(rec.gps != null && rec.meta.activity != ActivityKind.SKATING, rec.trend.lastElevationM, 0)
    }

    private inner class Stat(caption: String) : LinearLayout(this@LiveStats.context) {
        val label = TextView(context).apply { text = caption; textSize = 15f; alpha = 0.75f }
        private val value = TextView(context).apply {
            textSize = 46f
            typeface = Typeface.create(Typeface.DEFAULT, 600, false)
            includeFontPadding = false
        }

        init {
            orientation = VERTICAL
            addView(label)
            addView(value)
        }

        fun show(enabled: Boolean, number: Float, decimals: Int) {
            visibility = if (enabled) VISIBLE else GONE
            value.text = formatLiveValue(number, decimals)
        }
    }
}

internal fun formatLiveValue(value: Float, decimals: Int): String =
    if (value.isFinite()) "%.${decimals}f".format(value) else "—"
