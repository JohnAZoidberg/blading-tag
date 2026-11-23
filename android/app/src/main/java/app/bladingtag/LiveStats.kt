package app.bladingtag

import android.content.Context
import android.graphics.Typeface
import android.os.SystemClock
import android.util.TypedValue
import android.view.Gravity
import android.widget.LinearLayout
import android.widget.TextClock
import android.widget.TextView

/** Glanceable values for the sensors enabled in this recording. */
class LiveStats(context: Context) : LinearLayout(context) {
    private val elapsed = TextView(context).apply { textSize = 14f }
    private val heartRate = Stat("Heart rate · bpm")
    private val speed = Stat("Current speed · km/h")
    private val distance = Stat("Total distance · km")
    private val cadence = Stat("Cadence")
    private val elevation = Stat("Current elevation · m")

    init {
        orientation = VERTICAL
        val inset = (8 * resources.displayMetrics.density).toInt()
        setPadding(inset, inset / 2, inset, inset / 2)
        addView(LinearLayout(context).apply {
            gravity = Gravity.CENTER_VERTICAL
            addView(elapsed, LayoutParams(0, LayoutParams.WRAP_CONTENT, 1f))
            addView(TextClock(context).apply {
                textSize = 14f
            }, LayoutParams(LayoutParams.WRAP_CONTENT, LayoutParams.WRAP_CONTENT).apply {
                marginEnd = inset
            })
            addView(TextView(context).apply {
                text = "Swipe left for details"
                textSize = 14f
                alpha = 0.75f
            })
        })
        for (stat in listOf(heartRate, speed, distance, cadence, elevation)) {
            addView(stat, LayoutParams(LayoutParams.MATCH_PARENT, 0, 1f))
        }
    }

    fun update(rec: RecorderService.Recording) {
        val secs = (SystemClock.elapsedRealtimeNanos() - rec.startElapsedNs) / 1_000_000_000
        val time = "%d:%02d:%02d".format(secs / 3600, secs / 60 % 60, secs % 60)
        elapsed.text = if (rec.stopping) "$time  stopping…" else time
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
        val label = TextView(context).apply {
            text = caption
            textSize = 15f
            alpha = 0.75f
            gravity = Gravity.CENTER
            includeFontPadding = false
        }
        private val value = TextView(context).apply {
            typeface = Typeface.create(Typeface.DEFAULT, 600, false)
            fontFeatureSettings = "tnum"
            includeFontPadding = false
            gravity = Gravity.CENTER
            maxLines = 1
            textSize = 30f
        }

        init {
            orientation = VERTICAL
            addView(value, LayoutParams(LayoutParams.MATCH_PARENT, 0, 1f))
            addView(label, LayoutParams(LayoutParams.MATCH_PARENT, LayoutParams.WRAP_CONTENT))
        }

        fun show(enabled: Boolean, number: Float, decimals: Int) {
            visibility = if (enabled) VISIBLE else GONE
            val formatted = formatLiveValue(number, decimals)
            if (value.text == formatted) return
            if (number.isFinite()) {
                if (value.autoSizeTextType == TextView.AUTO_SIZE_TEXT_TYPE_NONE) {
                    value.setAutoSizeTextTypeUniformWithConfiguration(1, 700, 1, TypedValue.COMPLEX_UNIT_SP)
                }
            } else {
                value.setAutoSizeTextTypeWithDefaults(TextView.AUTO_SIZE_TEXT_TYPE_NONE)
                value.textSize = 30f
            }
            value.text = formatted
        }
    }
}

internal fun formatLiveValue(value: Float, decimals: Int): String =
    if (value.isFinite()) "%.${decimals}f".format(value) else "—"
