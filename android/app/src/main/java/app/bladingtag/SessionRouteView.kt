package app.bladingtag

import android.content.Context
import android.graphics.Canvas
import android.graphics.Paint
import android.graphics.Path
import android.view.View
import kotlin.math.PI
import kotlin.math.abs
import kotlin.math.cos
import kotlin.math.floor
import kotlin.math.log10
import kotlin.math.pow

/** North-up route map in local metres, available without a data connection. */
internal class SessionRouteView(context: Context, route: List<RecordedSession.Point>) : View(context) {
    private val density = resources.displayMetrics.density
    private val color = PlotView.palette(context)
    private val paint = Paint(Paint.ANTI_ALIAS_FLAG)
    private val path = Path()
    private val metresPerDegree = 111_195.0
    private val longitudeScale = metresPerDegree * cos(route.map { it.lat }.average() * PI / 180)
    private val x = DoubleArray(route.size)
    private val y = DoubleArray(route.size)
    private var pixelsPerMetre = 1.0
    private var scaleMetres = 1.0

    init {
        for (i in route.indices) {
            if (i > 0) {
                val change = route[i].lon - route[i - 1].lon
                x[i] = x[i - 1] + ((change + 540) % 360 - 180) * longitudeScale
            }
            y[i] = (route[i].lat - route[0].lat) * metresPerDegree
        }
        contentDescription = "Recorded GPS route, north up"
    }

    private var startX = 0f
    private var startY = 0f
    private var endX = 0f
    private var endY = 0f

    override fun onSizeChanged(w: Int, h: Int, oldw: Int, oldh: Int) {
        val padding = 32 * density
        val minX = x.min()
        val maxX = x.max()
        val minY = y.min()
        val maxY = y.max()
        pixelsPerMetre = minOf(
            (w - 2 * padding).coerceAtLeast(1f) / (maxX - minX).coerceAtLeast(25.0),
            (h - 2 * padding).coerceAtLeast(1f) / (maxY - minY).coerceAtLeast(25.0),
        )
        val target = 72 * density / pixelsPerMetre
        val step = 10.0.pow(floor(log10(target)))
        scaleMetres = listOf(5.0, 2.0, 1.0).first { it * step <= target } * step
        path.reset()
        for (i in x.indices) {
            val px = w / 2f + ((x[i] - (minX + maxX) / 2) * pixelsPerMetre).toFloat()
            val py = h / 2f - ((y[i] - (minY + maxY) / 2) * pixelsPerMetre).toFloat()
            if (i == 0) {
                startX = px
                startY = py
                path.moveTo(px, py)
            } else path.lineTo(px, py)
            endX = px
            endY = py
        }
    }

    override fun onDraw(canvas: Canvas) {
        val step = (scaleMetres * pixelsPerMetre).toFloat()
        paint.color = color[0]
        paint.alpha = 33
        paint.strokeWidth = density
        var gx = width / 2f % step
        while (gx < width) {
            canvas.drawLine(gx, 0f, gx, height.toFloat(), paint)
            gx += step
        }
        var gy = height / 2f % step
        while (gy < height) {
            canvas.drawLine(0f, gy, width.toFloat(), gy, paint)
            gy += step
        }

        paint.alpha = 255
        paint.style = Paint.Style.STROKE
        paint.strokeJoin = Paint.Join.ROUND
        paint.strokeCap = Paint.Cap.ROUND
        paint.strokeWidth = 3 * density
        canvas.drawPath(path, paint)
        paint.style = Paint.Style.FILL
        paint.color = color[2]
        canvas.drawCircle(startX, startY, 5 * density, paint)
        if (abs(startX - endX) + abs(startY - endY) > density) {
            paint.color = color[1]
            canvas.drawCircle(endX, endY, 5 * density, paint)
        }

        paint.color = color[0]
        paint.textSize = 14 * resources.displayMetrics.scaledDensity
        val left = 12 * density
        val bottom = height - 16 * density
        canvas.drawText("↑ N", left, 22 * density, paint)
        canvas.drawLine(left, bottom, left + step, bottom, paint)
        val label = if (scaleMetres >= 1000) "%.0f km".format(scaleMetres / 1000) else
            "%.0f m".format(scaleMetres)
        canvas.drawText(label, left, bottom - 6 * density, paint)
    }
}
