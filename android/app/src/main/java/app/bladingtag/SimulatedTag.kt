package app.bladingtag

import android.os.Handler
import android.os.HandlerThread
import android.os.SystemClock
import java.nio.ByteBuffer
import java.nio.ByteOrder
import kotlin.math.PI
import kotlin.math.max
import kotlin.math.sin
import kotlin.random.Random

/**
 * A fake tag for testing the app and the analysis without hardware (debug
 * builds only). It produces byte-exact Info, Status and Data payloads per
 * PROTOCOL.md, so the session files look like a real recording: 200 Hz
 * IMU6 in 19-sample packets, Status every second, a sample clock that runs
 * 0.4 % fast, and a crude stroke pattern at ~70 pushes/min.
 */
class SimulatedTag(listener: Listener) : TagLink(ADDRESS, listener) {

    private val thread = HandlerThread("tag-sim").apply { start() }
    private val handler = Handler(thread.looper)
    private val bootNs = SystemClock.elapsedRealtimeNanos() - 42_000_000_000L
    private var sessionId = 7
    private var recording = false
    private var seq = 0
    private var index = 0L
    private var t0Us = 0L
    private var recorded = 0L
    private val rnd = Random(1)

    override fun start() {
        handler.post {
            connected = true
            mtu = Protocol.MTU
            handleInfo(info())
            sessionId++
            recording = true
            seq = 0; index = 0; recorded = 0
            t0Us = tagNowUs()
            resetSequence()
            handleStatus(SystemClock.elapsedRealtimeNanos(), status())
            handler.postDelayed(::tick, PACKET_MS)
            handler.postDelayed(::statusTick, 1000)
        }
    }

    override fun stop(done: () -> Unit) {
        handler.post {
            recording = false
            handler.removeCallbacksAndMessages(null)
            handleStatus(SystemClock.elapsedRealtimeNanos(), status())
            connected = false
            thread.quitSafely()
            done()
        }
    }

    override fun command(bytes: ByteArray) {}

    private fun tagNowUs() = (SystemClock.elapsedRealtimeNanos() - bootNs) / 1000

    private fun statusTick() {
        handleStatus(SystemClock.elapsedRealtimeNanos(), status())
        handler.postDelayed(::statusTick, 1000)
    }

    /** Emit every whole packet's worth of samples due by now. */
    private fun tick() {
        val due = ((tagNowUs() - t0Us) * RATE_HZ / 1e6).toLong()
        while (due - index >= PER_PACKET) emitPacket()
        handler.postDelayed(::tick, PACKET_MS)
    }

    private fun emitPacket() {
        val b = ByteBuffer.allocate(Protocol.DATA_HEADER + PER_PACKET * Protocol.IMU6_SAMPLE)
            .order(ByteOrder.LITTLE_ENDIAN)
        b.put(Protocol.PACKET_IMU6.toByte()).put(PER_PACKET.toByte()).putShort(seq.toShort())
            .putInt(index.toInt()).putLong(t0Us + (index * 1e6 / RATE_HZ).toLong())
        repeat(PER_PACKET) { i ->
            val t = (index + i) / NOMINAL_HZ
            // Push phase: a sideways accel pulse and some roll, then glide.
            val phase = (t * STROKES_PER_S) % 1.0
            val push = if (phase < 0.4) sin(phase / 0.4 * PI) else 0.0
            val vib = rnd.nextDouble(-0.08, 0.08)
            fun g(v: Double) = (v * 2048).toInt().coerceIn(-32768, 32767).toShort()
            fun dps(v: Double) = (v * 16.384).toInt().coerceIn(-32768, 32767).toShort()
            b.putShort(g(0.1 * sin(2 * PI * t * 0.5) + vib))
            b.putShort(g(1.5 * push + vib))
            b.putShort(g(1.0 + 0.6 * push + vib))
            b.putShort(dps(120 * push * sin(phase / 0.4 * 2 * PI)))
            b.putShort(dps(15 * sin(2 * PI * t * STROKES_PER_S)))
            b.putShort(dps(5 * vib))
        }
        seq = (seq + 1) and 0xFFFF
        index += PER_PACKET
        recorded += PER_PACKET
        handleData(SystemClock.elapsedRealtimeNanos(), b.array())
    }

    private fun info(): ByteArray = ByteBuffer.allocate(12).order(ByteOrder.LITTLE_ENDIAN)
        .put(Protocol.VERSION.toByte()).put(0).put(1).put(0)
        .putShort(0x3f).put(0x0f).put(0x1f).putInt(8000).array()

    private fun status(): ByteArray = ByteBuffer.allocate(32).order(ByteOrder.LITTLE_ENDIAN)
        .put((if (recording) Protocol.STATE_RECORDING else Protocol.STATE_READY).toByte())
        .put(0)
        .putShort(max(2700, 3000 - (recorded / 20_000).toInt()).toShort())
        .putShort(sessionId.toShort())
        .putShort(NOMINAL_HZ.toInt().toShort())
        .put(16).put(1)
        .putShort(2000)
        .putInt(recorded.toInt()).putInt(0).putInt(0)
        .putLong(tagNowUs())
        .array()

    companion object {
        /** Locally administered, can't clash with a real tag's static random address. */
        const val ADDRESS = "02:00:00:00:00:01"
        const val NOMINAL_HZ = 200.0
        private const val RATE_HZ = NOMINAL_HZ * 1.004
        private const val PER_PACKET = 19
        private const val PACKET_MS = 50L
        private const val STROKES_PER_S = 70 / 60.0
    }
}
