package app.bladingtag

import kotlin.math.roundToInt
import kotlin.math.sqrt

/**
 * The last [SECONDS] of one tag's motion, averaged down to about [RATE_HZ],
 * for the live plots and the live push rate. Plain Kotlin, so it's unit
 * tested on the JVM.
 *
 * [TagLink] feeds it each new sample (after dropping resent ones); the UI and
 * the metrics tick read copies. A gap (samples lost) is stored as a row of
 * NaN, so plots break the line there and [cadence] doesn't reach across it.
 */
class LiveSignal {

    /** Copy of the newest samples: [t] in seconds before now (≤ 0), channels in g and °/s. */
    class Window(val rateHz: Float, val t: FloatArray, val ch: Array<FloatArray>) {
        val size get() = t.size
    }

    private val buf = Array(CHANNELS) { FloatArray(CAPACITY) }
    private var written = 0L        // rows written, ever
    private var lastGap = -1L       // row index of the newest gap row
    private val acc = FloatArray(CHANNELS)
    private var accN = 0
    private var factor = 0
    @Volatile var rateHz = 0f; private set

    /** One raw IMU6 sample (ax, ay, az, gx, gy, gz) at [odrHz] with the given ranges. */
    @Synchronized
    fun add(s: IntArray, odrHz: Int, accelRangeG: Int, gyroRangeDps: Int) {
        if (factor == 0) {
            factor = maxOf(1, (odrHz.toFloat() / RATE_HZ).roundToInt())
            rateHz = odrHz.toFloat() / factor
        }
        val a = accelRangeG / 32768f
        val g = gyroRangeDps / 32768f
        for (i in 0 until 3) acc[i] += s[i] * a
        for (i in 3 until 6) acc[i] += s[i] * g
        if (++accN < factor) return
        for (i in 0 until CHANNELS) {
            buf[i][(written % CAPACITY).toInt()] = acc[i] / accN
            acc[i] = 0f
        }
        accN = 0
        written++
    }

    /** Samples were lost before the next [add]. */
    @Synchronized
    fun gap() {
        if (written == 0L || written - 1 == lastGap) return
        for (i in 0 until CHANNELS) buf[i][(written % CAPACITY).toInt()] = Float.NaN
        lastGap = written
        written++
        acc.fill(0f)
        accN = 0
    }

    /** The newest [seconds] (at most [SECONDS]), oldest first. */
    @Synchronized
    fun window(seconds: Float): Window {
        val rate = rateHz
        val n = if (rate <= 0f) 0 else minOf(written, CAPACITY.toLong(), (seconds * rate).toLong()).toInt()
        val start = written - n
        val t = FloatArray(n) { (it - n + 1) / rate }
        val ch = Array(CHANNELS) { c ->
            FloatArray(n) { buf[c][((start + it) % CAPACITY).toInt()] }
        }
        return Window(rate, t, ch)
    }

    /** Repetition period of the newest [Cadence.WINDOW_S] without a gap, or null. */
    fun cadence(): Cadence? {
        val w = window(Cadence.WINDOW_S)
        if (w.size < (Cadence.WINDOW_S * w.rateHz).toInt() || w.ch[3].any { it.isNaN() }) return null
        return Cadence.estimate(w.ch[3], w.ch[4], w.ch[5], w.rateHz)
    }

    companion object {
        const val RATE_HZ = 50
        const val SECONDS = 30
        private const val CHANNELS = 6
        private const val CAPACITY = RATE_HZ * SECONDS
    }
}

/**
 * The dominant repetition period of the gyro vector, the same method as
 * `cadence_track` in host/src/bladetag/explore.py: autocorrelation summed
 * over the three axes (so it doesn't depend on how the tag is mounted), the
 * first peak that is nearly as strong as the best one.
 *
 * [periodS] is one cycle of the limb the tag is on (one stroke of that foot).
 * [strength] is the autocorrelation at that lag, 0..1: how repetitive the
 * window is. [gyroRmsDps] says whether there's motion at all.
 */
data class Cadence(val periodS: Float, val strength: Float, val gyroRmsDps: Float) {

    /** Cycles of the tagged limb per minute; [RateUnit] turns it into pushes, steps or rpm. */
    val cyclesPerMin get() = 60f / periodS

    /** Moving and repetitive: the thresholds `bladetag explore` uses. */
    val valid get() = gyroRmsDps > MIN_GYRO_RMS_DPS && strength > MIN_STRENGTH

    companion object {
        const val WINDOW_S = 8f
        const val MIN_PERIOD_S = 0.3f
        const val MAX_PERIOD_S = 3f
        const val MIN_GYRO_RMS_DPS = 30f
        const val MIN_STRENGTH = 0.5f

        fun estimate(gx: FloatArray, gy: FloatArray, gz: FloatArray, fs: Float): Cadence? {
            val n = gx.size
            val lo = maxOf(1, (MIN_PERIOD_S * fs).toInt())
            val hi = minOf((MAX_PERIOD_S * fs).toInt(), n / 2)
            if (hi - lo < 2) return null
            val axes = arrayOf(gx, gy, gz).map { a ->
                val m = a.average().toFloat()
                FloatArray(n) { a[it] - m }
            }
            val ac = FloatArray(hi + 2)
            for (x in axes) for (k in 0..hi + 1) {
                var s = 0f
                for (i in 0 until n - k) s += x[i] * x[i + k]
                ac[k] += s
            }
            val ac0 = ac[0]
            if (ac0 <= 0f) return null
            for (k in ac.indices) ac[k] /= ac0
            // Local maxima strictly inside [lo, hi], like scipy's find_peaks on ac[lo:hi+1].
            val peaks = (lo + 1 until hi).filter { ac[it] > ac[it - 1] && ac[it] >= ac[it + 1] }
            if (peaks.isEmpty()) return null
            // Multiples of the period also correlate; take the first peak
            // that is nearly as strong as the best one.
            val best = peaks.maxOf { ac[it] }
            val k = peaks.first { ac[it] >= 0.8f * best }
            // Parabolic interpolation between lags: at 50 Hz one lag is 2 %
            // of a 1 s period, too coarse for a rate display.
            val y0 = ac[k - 1]; val y1 = ac[k]; val y2 = ac[k + 1]
            val den = y0 - 2 * y1 + y2
            val dk = if (den < 0f) (0.5f * (y0 - y2) / den).coerceIn(-0.5f, 0.5f) else 0f
            val rms = sqrt(axes.sumOf { x -> x.sumOf { (it * it).toDouble() } } / n).toFloat()
            return Cadence((k + dk) / fs, y1, rms)
        }
    }
}

/**
 * How the live view names the repetition for an activity. The tag on one
 * foot sees one cycle per stroke, stride or crank turn of that foot:
 * skating pushes and walking steps alternate feet, so there are [perCycle]
 * = 2 of them per cycle; a crank turns once.
 */
class RateUnit(val perCycle: Int, val rate: String, val title: String, val one: String) {
    fun rate(cyclesPerMin: Float) = cyclesPerMin * perCycle
    /** Metres per push/step/turn at [kmh]. */
    fun distancePer(kmh: Float, cyclesPerMin: Float) = kmh / 3.6f / (rate(cyclesPerMin) / 60f)

    companion object {
        fun of(a: ActivityKind) = when (a) {
            ActivityKind.SKATING -> RateUnit(2, "pushes/min", "Pushes/min", "push")
            ActivityKind.WALKING -> RateUnit(2, "steps/min", "Steps/min", "step")
            ActivityKind.CYCLING -> RateUnit(1, "rpm", "Cadence, rpm", "crank turn")
            // Not given: one foot's cycles, without guessing what they are.
            ActivityKind.UNKNOWN -> RateUnit(1, "cycles/min", "Cycles/min (one foot)", "cycle")
        }
    }
}

/**
 * One row per second of the live numbers, for the session trend charts.
 * Cycles/min (of the tagged foot, see [RateUnit]) and speed are NaN when
 * unknown (not moving, no GPS fix).
 * Written and read on the main thread.
 */
class Trend {
    var size = 0; private set
    var t = FloatArray(1024); private set
    var cyclesPerMin = FloatArray(1024); private set
    var speedKmh = FloatArray(1024); private set

    fun add(tS: Float, cycles: Float, speed: Float) {
        if (size == t.size) {
            t = t.copyOf(size * 2)
            cyclesPerMin = cyclesPerMin.copyOf(size * 2)
            speedKmh = speedKmh.copyOf(size * 2)
        }
        t[size] = tS
        cyclesPerMin[size] = cycles
        speedKmh[size] = speed
        size++
    }

    val lastCyclesPerMin get() = if (size == 0) Float.NaN else cyclesPerMin[size - 1]
    val lastSpeedKmh get() = if (size == 0) Float.NaN else speedKmh[size - 1]
}
