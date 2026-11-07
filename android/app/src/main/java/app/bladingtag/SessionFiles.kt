package app.bladingtag

import java.io.BufferedOutputStream
import java.io.File
import java.io.FileOutputStream
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.TimeZone

/**
 * Session file format, see PROTOCOL.md §2. Plain java.io so it runs in unit
 * tests; the callers take care of threading (each log is used from one
 * thread at a time, but [TagLog.append] is synchronized anyway since
 * flushing happens from a timer).
 */

/** tag-ADDRESS.bin: (u64 elapsed_ns, u8 kind, u16 length, payload) records. */
class TagLog(val file: File) {
    private val out = BufferedOutputStream(FileOutputStream(file, true), 64 * 1024)
    private val header = ByteBuffer.allocate(HEADER).order(ByteOrder.LITTLE_ENDIAN)
    var records = 0L
        private set
    var bytes = file.length()
        private set
    private var closed = false

    /** Ignored after [close]: a link may still deliver a packet while stopping. */
    @Synchronized
    fun append(elapsedNs: Long, kind: Int, payload: ByteArray) {
        if (closed) return
        header.clear()
        header.putLong(elapsedNs).put(kind.toByte()).putShort(payload.size.toShort())
        out.write(header.array())
        out.write(payload)
        records++
        bytes += HEADER + payload.size
    }

    @Synchronized fun flush() { if (!closed) out.flush() }
    @Synchronized fun close() { if (!closed) { closed = true; out.close() } }

    companion object {
        const val HEADER = 11
        const val KIND_DATA = 1
        const val KIND_STATUS = 2

        /** "C0:BB:4A:A7:0D:57" -> "tag-C0BB4AA70D57.bin" */
        fun fileName(address: String) = "tag-" + address.replace(":", "").uppercase() + ".bin"
    }
}

/** gps.csv, one row per fix. Empty fields for what the phone didn't give. */
class GpsLog(val file: File) {
    private val out = file.bufferedWriter()
    var fixes = 0L
        private set

    init {
        out.write("elapsed_ns,utc_ms,lat,lon,alt_m,speed_mps,bearing_deg,h_acc_m,speed_acc_mps\n")
    }

    class Fix(
        val elapsedNs: Long, val utcMs: Long, val lat: Double, val lon: Double,
        val altM: Double?, val speedMps: Float?, val bearingDeg: Float?,
        val hAccM: Float?, val speedAccMps: Float?,
    )

    private var closed = false

    @Synchronized
    fun append(f: Fix) {
        if (closed) return
        fun o(v: Any?) = v?.toString() ?: ""
        out.write("${f.elapsedNs},${f.utcMs},${f.lat},${f.lon},${o(f.altM)},${o(f.speedMps)}," +
            "${o(f.bearingDeg)},${o(f.hAccM)},${o(f.speedAccMps)}\n")
        fixes++
    }

    @Synchronized fun flush() { if (!closed) out.flush() }
    @Synchronized fun close() { if (!closed) { closed = true; out.close() } }
}

/** What session.json says about one tag. Fields are filled in as the tag reports them. */
class TagMeta(val address: String) {
    var position = "unknown"
    var infoHex: String? = null
    var sessionId: Int? = null
    var odrHz: Int? = null
    var accelRangeG: Int? = null
    var gyroRangeDps: Int? = null
    val file get() = TagLog.fileName(address)
}

class SessionMeta(
    val recorderVersion: String,
    val startUtcMs: Long,
    val startElapsedNs: Long,
    val tags: List<TagMeta>,
) {
    var endElapsedNs: Long? = null
    var notes = ""

    fun toJson(): String {
        val sb = StringBuilder()
        sb.append("{\n")
        sb.append("  \"format_version\": 1,\n")
        sb.append("  \"recorder\": { \"name\": \"blading-tag-android\", \"version\": ${q(recorderVersion)} },\n")
        sb.append("  \"start_utc\": ${q(isoUtc(startUtcMs))},\n")
        sb.append("  \"start_elapsed_ns\": $startElapsedNs,\n")
        endElapsedNs?.let { sb.append("  \"end_elapsed_ns\": $it,\n") }
        sb.append("  \"tags\": [")
        tags.forEachIndexed { i, t ->
            sb.append(if (i == 0) "\n" else ",\n")
            val fields = listOfNotNull(
                "\"address\": ${q(t.address)}",
                "\"position\": ${q(t.position)}",
                "\"file\": ${q(t.file)}",
                t.infoHex?.let { "\"info_hex\": ${q(it)}" },
                t.sessionId?.let { "\"session_id\": $it" },
                t.odrHz?.let { "\"odr_hz\": $it" },
                t.accelRangeG?.let { "\"accel_range_g\": $it" },
                t.gyroRangeDps?.let { "\"gyro_range_dps\": $it" },
            )
            sb.append("    {\n      ").append(fields.joinToString(",\n      ")).append("\n    }")
        }
        sb.append(if (tags.isEmpty()) "],\n" else "\n  ],\n")
        sb.append("  \"notes\": ${q(notes)}\n")
        sb.append("}\n")
        return sb.toString()
    }

    companion object {
        fun isoUtc(ms: Long): String = SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ss.SSS'Z'", Locale.US)
            .apply { timeZone = TimeZone.getTimeZone("UTC") }.format(Date(ms))

        /** "session-20261107-142530" (UTC, like start_utc). */
        fun folderName(startUtcMs: Long): String = "session-" +
            SimpleDateFormat("yyyyMMdd-HHmmss", Locale.US)
                .apply { timeZone = TimeZone.getTimeZone("UTC") }.format(Date(startUtcMs))

        fun q(s: String): String {
            val sb = StringBuilder("\"")
            for (c in s) when {
                c == '"' -> sb.append("\\\"")
                c == '\\' -> sb.append("\\\\")
                c == '\n' -> sb.append("\\n")
                c == '\r' -> sb.append("\\r")
                c == '\t' -> sb.append("\\t")
                c < ' ' -> sb.append("\\u%04x".format(c.code))
                else -> sb.append(c)
            }
            return sb.append('"').toString()
        }
    }
}

/** Writes session.json atomically, so a crash never leaves it half written. */
fun writeSessionJson(dir: File, meta: SessionMeta) {
    val tmp = File(dir, "session.json.tmp")
    tmp.writeText(meta.toJson())
    tmp.renameTo(File(dir, "session.json"))
}
