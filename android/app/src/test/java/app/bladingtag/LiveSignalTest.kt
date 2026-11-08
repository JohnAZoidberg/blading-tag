package app.bladingtag

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File
import kotlin.math.PI
import kotlin.math.abs
import kotlin.math.sin
import kotlin.random.Random

class LiveSignalTest {

    /** Stroke-like gyro: a roll pulse in the first 40 % of each cycle, noise, a slow yaw. */
    private fun gyro(t: Double, period: Double, rnd: Random): DoubleArray {
        val phase = (t / period) % 1.0
        val push = if (phase < 0.4) sin(phase / 0.4 * PI) else 0.0
        return doubleArrayOf(
            150 * push * sin(phase / 0.4 * 2 * PI) + rnd.nextDouble(-10.0, 10.0),
            20 * sin(2 * PI * t / period) + rnd.nextDouble(-10.0, 10.0),
            8 * sin(2 * PI * t / 7.3) + rnd.nextDouble(-10.0, 10.0),
        )
    }

    /** Raw IMU6 at [odr] Hz, ±16 g / ±2000 dps, as the tag sends it. */
    private fun feed(live: LiveSignal, seconds: Double, period: Double, odr: Int = 200, t0: Double = 0.0) {
        val rnd = Random(3)
        val n = (seconds * odr).toInt()
        for (i in 0 until n) {
            val t = t0 + i.toDouble() / odr
            val g = gyro(t, period, rnd)
            val raw = intArrayOf(0, 0, 2048,
                (g[0] * 16.384).toInt(), (g[1] * 16.384).toInt(), (g[2] * 16.384).toInt())
            live.add(raw, odr, 16, 2000)
        }
    }

    @Test
    fun cadenceFindsTheStrokePeriod() {
        for (period in listOf(0.9, 1.3, 1.7, 2.4)) {
            val rnd = Random(1)
            val fs = 50f
            val n = (Cadence.WINDOW_S * fs).toInt()
            val g = Array(3) { FloatArray(n) }
            for (i in 0 until n) gyro(i / fs.toDouble(), period, rnd).forEachIndexed { a, v -> g[a][i] = v.toFloat() }
            val c = Cadence.estimate(g[0], g[1], g[2], fs)!!
            assertEquals("period $period", period, c.periodS.toDouble(), period * 0.015)
            assertTrue("strength ${c.strength}", c.valid)
        }
    }

    @Test
    fun noiseIsNotACadence() {
        val rnd = Random(2)
        val n = 400
        val g = Array(3) { FloatArray(n) { rnd.nextFloat() * 200 - 100 } }
        val c = Cadence.estimate(g[0], g[1], g[2], 50f)
        assertTrue(c == null || !c.valid)
    }

    @Test
    fun standingStillIsNotACadence() {
        val rnd = Random(2)
        val g = Array(3) { FloatArray(400) { rnd.nextFloat() * 2 - 1 } }
        val c = Cadence.estimate(g[0], g[1], g[2], 50f)
        assertTrue(c == null || !c.valid)
    }

    @Test
    fun decimatesTo50HzAndKeepsThirtySeconds() {
        val live = LiveSignal()
        feed(live, 40.0, 1.5)
        assertEquals(50f, live.rateHz)
        val w = live.window(100f)
        assertEquals(LiveSignal.RATE_HZ * LiveSignal.SECONDS, w.size)
        assertEquals(0f, w.t.last())
        assertEquals(1f, w.ch[2].average().toFloat(), 1e-3f)   // az = 1 g
        val c = live.cadence()!!
        assertEquals(1.5, c.periodS.toDouble(), 0.02)
        assertEquals(80f, c.pushesPerMin, 1.2f)
    }

    @Test
    fun lowRateIsNotDecimated() {
        val live = LiveSignal()
        feed(live, 10.0, 1.5, odr = 25)
        assertEquals(25f, live.rateHz)
        assertNotNull(live.cadence())
    }

    @Test
    fun noCadenceAcrossAGap() {
        val live = LiveSignal()
        feed(live, 10.0, 1.5)
        assertNotNull(live.cadence())
        live.gap()
        live.gap()   // a second gap in a row adds nothing
        assertEquals(1, live.window(30f).ch[3].count { it.isNaN() })
        feed(live, 5.0, 1.5, t0 = 12.0)
        assertNull(live.cadence())
        feed(live, 4.0, 1.5, t0 = 17.0)
        assertNotNull(live.cadence())
    }

    @Test
    fun emptySignal() {
        val live = LiveSignal()
        assertEquals(0, live.window(10f).size)
        assertNull(live.cadence())
        live.gap()
        assertEquals(0, live.window(10f).size)
    }

    @Test
    fun trendGrows() {
        val tr = Trend()
        assertTrue(tr.lastPushesPerMin.isNaN())
        for (i in 0 until 5000) tr.add(i.toFloat(), if (i % 2 == 0) Float.NaN else 60f, 20f)
        assertEquals(5000, tr.size)
        assertEquals(60f, tr.lastPushesPerMin)
        assertEquals(4999f, tr.t[4999])
        assertFalse(tr.pushesPerMin[4998] == 60f)
    }

    /**
     * Same answer as `cadence_track` in host/src/bladetag/explore.py on a
     * real recording. Skipped unless LIVE_PARITY_CSV names a file with
     * a "#fs <Hz>" line, lines "gx,gy,gz" (°/s, decimated to about 50 Hz),
     * and "# t_s,period_s,strength" rows from the host. host/live_parity.py
     * writes one from a session folder.
     */
    @Test
    fun matchesHostCadenceTrack() {
        val path = System.getenv("LIVE_PARITY_CSV") ?: return
        val lines = File(path).readLines()
        val samples = lines.filter { !it.startsWith("#") }.map { l -> l.split(",").map { it.toFloat() } }
        val host = lines.filter { it.startsWith("# ") }.map { l -> l.drop(2).split(",").map { it.toFloat() } }
        val fs = lines.first { it.startsWith("#fs ") }.drop(4).toFloat()
        val n = (Cadence.WINDOW_S * fs).toInt()
        var compared = 0
        var agree = 0
        for ((t, period, strength) in host) {
            val i0 = (t * fs).toInt() - n / 2
            if (i0 < 0 || i0 + n > samples.size || strength < Cadence.MIN_STRENGTH) continue
            val g = Array(3) { a -> FloatArray(n) { samples[i0 + it][a] } }
            val c = Cadence.estimate(g[0], g[1], g[2], fs) ?: continue
            compared++
            if (abs(c.periodS / period - 1) < 0.05) agree++
        }
        println("parity: $agree of $compared repetitive windows within 5 %")
        assertTrue(compared > 0)
        assertTrue("$agree of $compared", agree >= 0.9 * compared)
    }
}
