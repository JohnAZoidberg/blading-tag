package app.bladingtag

import android.content.Context
import android.content.res.Configuration
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.Paint
import android.graphics.Path
import android.view.View
import kotlin.math.ceil
import kotlin.math.floor
import kotlin.math.log10
import kotlin.math.max
import kotlin.math.pow

/**
 * A small line chart drawn on a Canvas: a title with a legend, a few y grid
 * lines with labels, and one or more series sharing one y axis. NaN in a
 * series breaks its line. With more points than pixels, each pixel column
 * shows the mean of its points.
 *
 * The y range follows the data in steps of 1/2/5 × 10ⁿ and only shrinks once
 * the data falls well below it, so a live plot doesn't jump every frame.
 */
class PlotView(context: Context) : View(context) {

    class Series(val label: String, val color: Int, val y: FloatArray)

    var title = ""
    /** Legend shown when there are two or more series. */
    private var series: List<Series> = emptyList()
    private var x = FloatArray(0)
    private var xMin = 0f
    private var xMax = 1f
    /** Range always includes zero (rates, speeds). Otherwise symmetric about zero. */
    var fromZero = true
    /** y range never smaller than this. */
    var minSpan = 1f
    var xLabel: (Float) -> String = { "" }
    var emptyText = "waiting…"

    private var top = Float.NaN

    private val dark = (context.resources.configuration.uiMode and Configuration.UI_MODE_NIGHT_MASK) ==
        Configuration.UI_MODE_NIGHT_YES
    private val ink = if (dark) Color.parseColor("#c3c2b7") else Color.parseColor("#52514e")
    private val density = context.resources.displayMetrics.density
    private val scaled = context.resources.displayMetrics.scaledDensity

    private val textPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply { color = ink; textSize = 12 * scaled }
    private val gridPaint = Paint().apply {
        color = ink; alpha = 50; strokeWidth = max(1f, density / 2)
    }
    private val linePaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.STROKE; strokeWidth = 2 * density
        strokeJoin = Paint.Join.ROUND; strokeCap = Paint.Cap.ROUND
    }
    private val dotPaint = Paint(Paint.ANTI_ALIAS_FLAG)
    private val path = Path()

    fun set(x: FloatArray, series: List<Series>, xMin: Float, xMax: Float) {
        this.x = x
        this.series = series
        this.xMin = xMin
        this.xMax = if (xMax > xMin) xMax else xMin + 1
        invalidate()
    }

    override fun onDraw(c: Canvas) {
        val lineH = textPaint.fontSpacing
        val left = textPaint.measureText("0000")
        val plotTop = lineH * 2.2f
        val plotBottom = height - lineH * 1.2f
        val right = width - 2 * density

        // Title and legend
        var tx = 0f
        textPaint.textAlign = Paint.Align.LEFT
        c.drawText(title, tx, lineH, textPaint)
        tx += textPaint.measureText(title) + 12 * density
        if (series.size > 1) for (s in series) {
            linePaint.color = s.color
            c.drawLine(tx, lineH * 0.65f, tx + 12 * density, lineH * 0.65f, linePaint)
            tx += 16 * density
            c.drawText(s.label, tx, lineH, textPaint)
            tx += textPaint.measureText(s.label) + 12 * density
        }

        // y range
        var dataMax = 0f
        var dataMin = 0f
        var any = false
        for (s in series) for (v in s.y) if (!v.isNaN()) {
            any = true
            if (v > dataMax) dataMax = v
            if (v < dataMin) dataMin = v
        }
        val need = max(minSpan, if (fromZero) dataMax else max(dataMax, -dataMin))
        if (top.isNaN() || need > top || need < 0.4f * top) top = niceCeil(need)
        val yLo = if (fromZero) 0f else -top
        val step = niceCeil((top - yLo) / 3)
        fun py(v: Float) = plotBottom - (v - yLo) / (top - yLo) * (plotBottom - plotTop)
        fun px(v: Float) = left + (v - xMin) / (xMax - xMin) * (right - left)

        textPaint.textAlign = Paint.Align.RIGHT
        var g = ceil(yLo / step) * step
        while (g <= top + step * 1e-3f) {
            val y = py(g)
            c.drawLine(left, y, right, y, gridPaint)
            c.drawText(fmt(g, step), left - 4 * density, y + lineH * 0.3f, textPaint)
            g += step
        }
        textPaint.textAlign = Paint.Align.LEFT
        c.drawText(xLabel(xMin), left, height - lineH * 0.2f, textPaint)
        textPaint.textAlign = Paint.Align.RIGHT
        c.drawText(xLabel(xMax), right, height - lineH * 0.2f, textPaint)

        if (!any) {
            textPaint.textAlign = Paint.Align.CENTER
            c.drawText(emptyText, (left + right) / 2, (plotTop + plotBottom) / 2, textPaint)
            return
        }

        val cols = max(1, (right - left).toInt())
        for (s in series) {
            linePaint.color = s.color
            dotPaint.color = s.color
            path.reset()
            var open = false
            var runLen = 0
            var lastX = 0f
            var lastY = 0f
            fun point(xx: Float, v: Float) {
                if (v.isNaN()) {
                    if (runLen == 1) c.drawCircle(lastX, lastY, 2 * density, dotPaint)
                    open = false; runLen = 0
                    return
                }
                val X = px(xx); val Y = py(v.coerceIn(yLo, top))
                if (open) path.lineTo(X, Y) else path.moveTo(X, Y)
                open = true; runLen++; lastX = X; lastY = Y
            }
            val n = minOf(x.size, s.y.size)
            if (n <= cols) {
                for (i in 0 until n) point(x[i], s.y[i])
            } else {
                // Mean per pixel column; a column with no number breaks the line.
                var i = 0
                for (col in 0 until cols) {
                    val xEnd = xMin + (col + 1f) / cols * (xMax - xMin)
                    var sum = 0.0; var cnt = 0; var xs = 0.0; var m = 0
                    while (i < n && (x[i] < xEnd || col == cols - 1)) {
                        if (!s.y[i].isNaN()) { sum += s.y[i]; cnt++ }
                        xs += x[i]; m++; i++
                    }
                    if (m > 0) point((xs / m).toFloat(), if (cnt > 0) (sum / cnt).toFloat() else Float.NaN)
                }
            }
            point(0f, Float.NaN)
            c.drawPath(path, linePaint)
        }
    }

    private fun fmt(v: Float, step: Float) =
        if (step >= 1f || v == 0f) "%.0f".format(v) else "%.1f".format(v)

    companion object {
        /** Smallest 1, 2 or 5 × 10ⁿ ≥ v. */
        fun niceCeil(v: Float): Float {
            if (v <= 0f) return 1f
            val p = 10f.pow(floor(log10(v)))
            val m = v / p
            val nice = when {
                m <= 1.0001f -> 1f
                m <= 2.0001f -> 2f
                m <= 5.0001f -> 5f
                else -> 10f
            }
            return nice * p
        }

        /** Categorical slots 1-3 of the dataviz reference palette (validated together). */
        fun palette(context: Context): IntArray {
            val dark = (context.resources.configuration.uiMode and Configuration.UI_MODE_NIGHT_MASK) ==
                Configuration.UI_MODE_NIGHT_YES
            return (if (dark) listOf("#3987e5", "#d95926", "#199e70")
                    else listOf("#2a78d6", "#eb6834", "#1baf7a")).map { Color.parseColor(it) }.toIntArray()
        }
    }
}
