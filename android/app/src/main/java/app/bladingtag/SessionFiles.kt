package app.bladingtag

import java.io.BufferedOutputStream
import java.io.File
import java.io.FileOutputStream
import org.json.JSONObject
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.TimeZone

/**
 * Session file format, see PROTOCOL.md §2. Plain java.io (and org.json) so it
 * runs in unit tests; the callers take care of threading (each log is used from one
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

    /**
     * Returns false, and writes nothing, after [close]: a link may still
     * deliver a packet while stopping, or while the recording is split.
     */
    @Synchronized
    fun append(elapsedNs: Long, kind: Int, payload: ByteArray): Boolean {
        if (closed) return false
        header.clear()
        header.putLong(elapsedNs).put(kind.toByte()).putShort(payload.size.toShort())
        out.write(header.array())
        out.write(payload)
        records++
        bytes += HEADER + payload.size
        return true
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
    /** Fake data from [SimulatedTag], not a real recording. */
    var simulated = false
    val file get() = TagLog.fileName(address)

    /** For the next session when a recording is split: same tag, same settings. */
    fun copy() = TagMeta(address).also {
        it.position = position
        it.infoHex = infoHex
        it.sessionId = sessionId
        it.odrHz = odrHz
        it.accelRangeG = accelRangeG
        it.gyroRangeDps = gyroRangeDps
        it.simulated = simulated
    }
}

/**
 * What a session is (session.json "activity", PROTOCOL.md §2.2), with the
 * words the app uses for it. [gear] names the "gear" field for it.
 */
enum class ActivityKind(val label: String, val gear: String, val gearHint: String, val verb: String,
                    val moving: String) {
    SKATING("Skating", "Skates", "e.g. FR 90 mm wheels", "skate", "skate"),
    WALKING("Walking", "Shoes", "e.g. Brooks Ghost 15", "walk", "foot"),
    CYCLING("Cycling", "Bike", "e.g. road bike", "ride", "foot"),
    UNKNOWN("Not given", "Gear", "", "start", "tag");

    /** The session.json label: "skating", ... */
    val key get() = name.lowercase()

    companion object {
        /** UNKNOWN for null, "" or a label this version doesn't know. */
        fun of(key: String?): ActivityKind = entries.firstOrNull { it.key == key?.trim()?.lowercase() } ?: UNKNOWN
        /** The ones offered when starting a recording. */
        val PICKABLE = listOf(SKATING, WALKING, CYCLING)
    }
}

/** session.json "end_reason" values (PROTOCOL.md §2.2) and how the app words them. */
object EndReason {
    const val STOP = "stop"
    const val NOTIFICATION = "notification"
    const val TAG_BUTTON = "tag_button"
    const val SPLIT = "split"
    const val SERVICE_DESTROYED = "service_destroyed"
    const val CRASH = "crash"
    const val KILLED = "killed"

    fun label(reason: String?): String? = when (reason) {
        null -> null
        STOP -> "stopped"
        NOTIFICATION -> "stopped from the notification"
        TAG_BUTTON -> "stopped on the tag"
        SPLIT -> "split"
        "duration" -> "time limit"
        "link_lost" -> "tag lost"
        SERVICE_DESTROYED -> "stopped by Android"
        CRASH -> "app crashed"
        KILLED -> "app killed"
        else -> reason
    }
}

class SessionMeta(
    val recorderVersion: String,
    val startUtcMs: Long,
    val startElapsedNs: Long,
    val tags: List<TagMeta>,
) {
    var endElapsedNs: Long? = null
    /** Why it ended, one of [EndReason]'s keys; written with [endElapsedNs]. */
    var endReason: String? = null
    /** More about [endReason], e.g. the crash. */
    var endDetail: String? = null
    var notes = ""
    var activity = ActivityKind.UNKNOWN
    /** The shoes, skates or bike, as typed; "" if not given. */
    var gear = ""
    var heartRateEnabled = false
    var heartRate: HeartRateMeta? = null

    fun toJson(): String {
        val sb = StringBuilder()
        sb.append("{\n")
        sb.append("  \"format_version\": 1,\n")
        sb.append("  \"recorder\": { \"name\": \"blading-tag-android\", \"version\": ${q(recorderVersion)} },\n")
        sb.append("  \"start_utc\": ${q(isoUtc(startUtcMs))},\n")
        sb.append("  \"start_elapsed_ns\": $startElapsedNs,\n")
        endElapsedNs?.let { sb.append("  \"end_elapsed_ns\": $it,\n") }
        endReason?.let { sb.append("  \"end_reason\": ${q(it)},\n") }
        endDetail?.let { sb.append("  \"end_detail\": ${q(it)},\n") }
        if (heartRateEnabled) sb.append("  \"heart_rate\": ${heartRate?.json() ?: "{\"file\": \"heartrate.csv\"}"},\n")
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
                "\"simulated\": ${t.simulated}",
            )
            sb.append("    {\n      ").append(fields.joinToString(",\n      ")).append("\n    }")
        }
        sb.append(if (tags.isEmpty()) "],\n" else "\n  ],\n")
        sb.append("  \"notes\": ${q(notes)},\n")
        sb.append("  \"activity\": ${q(activity.key)},\n")
        sb.append("  \"gear\": ${q(gear)}\n")
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
fun writeSessionJson(dir: File, meta: SessionMeta) = writeSessionJson(dir, meta.toJson())

private fun writeSessionJson(dir: File, text: String) {
    val tmp = File(dir, "session.json.tmp")
    tmp.writeText(text)
    tmp.renameTo(File(dir, "session.json"))
}

/** What the session list shows from a session.json, and what it lets the user change. */
class SessionJson(
    val startElapsedNs: Long?,
    val endElapsedNs: Long?,
    val tags: List<Tag>,
    /** Shown as the session's description. */
    val notes: String,
    val activity: ActivityKind = ActivityKind.UNKNOWN,
    val gear: String = "",
    /** session.json "end_reason" (see [EndReason]), null if not given. */
    val endReason: String? = null,
    val endDetail: String? = null,
) {
    class Tag(val address: String, val position: String, val simulated: Boolean)

    /** Any of its tags is simulated: not a real recording. */
    val simulated get() = tags.any { it.simulated }

    companion object {
        /** The Android simulated tag's and the host fake tag's addresses (PROTOCOL.md §2.2). */
        fun isSimulatorAddress(address: String) =
            address == SimulatedTag.ADDRESS || address.uppercase().startsWith("FA:KE:")

        /** Null if the folder has no readable session.json. */
        fun read(dir: File): SessionJson? = runCatching {
            parse(File(dir, "session.json").readText())
        }.getOrNull()

        fun parse(text: String): SessionJson {
            val o = JSONObject(text)
            fun long(key: String) = if (o.isNull(key)) null else o.getLong(key)
            val tags = o.optJSONArray("tags")
            return SessionJson(
                startElapsedNs = long("start_elapsed_ns"),
                endElapsedNs = long("end_elapsed_ns"),
                tags = (0 until (tags?.length() ?: 0)).map { i ->
                    val t = tags!!.getJSONObject(i)
                    val address = t.getString("address")
                    Tag(address, Protocol.normalizePosition(t.optString("position", "unknown")),
                        // Sessions from before the field: known simulator addresses.
                        t.optBoolean("simulated", false) || isSimulatorAddress(address))
                },
                notes = o.optString("notes", ""),
                activity = ActivityKind.of(o.optString("activity", "")),
                gear = o.optString("gear", "").trim(),
                endReason = o.optString("end_reason", "").ifEmpty { null },
                endDetail = o.optString("end_detail", "").ifEmpty { null },
            )
        }

        /**
         * Records why a session that has no end ended, learnt afterwards
         * (the process was killed). Every other field stays as it is.
         */
        fun setEndReason(dir: File, reason: String, detail: String?) {
            val o = JSONObject(File(dir, "session.json").readText())
            o.put("end_reason", reason)
            detail?.let { o.put("end_detail", it) }
            writeSessionJson(dir, o.toString(2) + "\n")
        }

        /**
         * Sets the notes, activity, gear and the tag positions ([positions]: address ->
         * body location name, e.g. "left_wrist") of a finished session. Every other field
         * stays as it is, including ones this app doesn't know.
         */
        fun edit(dir: File, notes: String, positions: Map<String, String>,
                 activity: ActivityKind? = null, gear: String? = null) {
            val o = JSONObject(File(dir, "session.json").readText())
            o.put("notes", notes)
            activity?.let { o.put("activity", it.key) }
            gear?.let { o.put("gear", it.trim()) }
            val tags = o.optJSONArray("tags")
            for (i in 0 until (tags?.length() ?: 0)) {
                val t = tags!!.getJSONObject(i)
                positions[t.optString("address")]?.let { t.put("position", it) }
            }
            writeSessionJson(dir, o.toString(2) + "\n")
        }
    }
}
