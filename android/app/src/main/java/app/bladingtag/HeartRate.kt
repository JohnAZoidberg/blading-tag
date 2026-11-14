package app.bladingtag

import java.io.File
import java.util.UUID

/** Standard Heart Rate Measurement, retaining RR in its native 1/1024 s units. */
object HeartRate {
    val SERVICE: UUID = UUID.fromString("0000180d-0000-1000-8000-00805f9b34fb")
    val MEASUREMENT: UUID = UUID.fromString("00002a37-0000-1000-8000-00805f9b34fb")
    val CCCD: UUID = UUID.fromString("00002902-0000-1000-8000-00805f9b34fb")
    data class Measurement(val bpm: Int, val contactSupported: Boolean, val contactDetected: Boolean?,
                           val energyKj: Int?, val rrTicks: List<Int>)
    fun decode(raw: ByteArray): Measurement {
        require(raw.isNotEmpty()) { "empty measurement" }
        var offset = 1
        fun take(n: Int): Int {
            require(offset + n <= raw.size) { "truncated measurement" }
            var v = 0
            repeat(n) { v = v or ((raw[offset++].toInt() and 255) shl (8 * it)) }
            return v
        }
        val flags = raw[0].toInt() and 255
        val bpm = take(if (flags and 1 != 0) 2 else 1)
        val energy = if (flags and 8 != 0) take(2) else null
        val rr = mutableListOf<Int>()
        if (flags and 16 != 0) {
            require(offset < raw.size) { "missing RR interval" }
            while (offset < raw.size) rr.add(take(2))
        }
        require(offset == raw.size) { "unexpected trailing bytes" }
        val supported = flags and 4 != 0
        return Measurement(bpm, supported, if (supported) flags and 2 != 0 else null, energy, rr)
    }
}

class HeartRateMeta(val address: String, val name: String) {
    fun json() = "{\"address\": ${SessionMeta.q(address)}, \"name\": ${SessionMeta.q(name)}, \"file\": \"heartrate.csv\"}"
}

/** Raw bytes remain available even when a measurement cannot be decoded. */
class HeartRateLog(file: File) {
    private val out = file.bufferedWriter()
    private var closed = false
    init { out.write("elapsed_ns,bpm,contact_supported,contact_detected,energy_kj,rr_ticks,raw_hex\n") }
    @Synchronized fun append(ns: Long, raw: ByteArray) {
        if (closed) return
        val m = runCatching { HeartRate.decode(raw) }.getOrNull()
        val fields = if (m == null) ",,,,," else
            "${m.bpm},${m.contactSupported},${m.contactDetected ?: ""},${m.energyKj ?: ""},\"${m.rrTicks.joinToString(",", "[", "]")}\","
        out.write("$ns,$fields${raw.joinToString("") { "%02x".format(it.toInt() and 255) }}\n")
    }
    @Synchronized fun flush() { if (!closed) out.flush() }
    @Synchronized fun close() { if (!closed) { closed = true; out.close() } }
}
