package app.bladingtag

import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.util.UUID

/**
 * BLE protocol v1, see PROTOCOL.md §1. Decoding only; the recorder writes
 * what the tag sends verbatim and these are just for the live display.
 *
 * Every parser ignores trailing bytes it doesn't know (compatible changes
 * append fields) and returns null if the payload is too short.
 */
object Protocol {
    const val VERSION = 1

    private fun uuid(short: Int) =
        UUID.fromString("b1ad%04x-0e83-479a-bb08-19dab75f76db".format(short))

    val SERVICE: UUID = uuid(0x0001)
    val INFO: UUID = uuid(0x0002)
    val CONTROL: UUID = uuid(0x0003)
    val STATUS: UUID = uuid(0x0004)
    val DATA: UUID = uuid(0x0005)
    val CCCD: UUID = UUID.fromString("00002902-0000-1000-8000-00805f9b34fb")

    const val COMPANY_ID = 0xFFFF
    const val MTU = 247

    const val STATE_READY = 0
    const val STATE_RECORDING = 1

    const val FLAG_IMU_FAULT = 1
    const val FLAG_OVERFLOW = 2
    const val FLAG_LOW_BATTERY = 4

    const val OP_START = 0x01
    const val OP_STOP = 0x02
    const val OP_IDENTIFY = 0x03
    const val OP_SET_POSITION = 0x04
    const val OP_SLEEP = 0x05

    const val PACKET_IMU6 = 0x01
    const val DATA_HEADER = 16
    const val IMU6_SAMPLE = 12

    fun stateName(s: Int) = when (s) {
        STATE_READY -> "ready"
        STATE_RECORDING -> "recording"
        else -> "state $s"
    }

    /** Position as used in session.json. */
    fun positionName(p: Int) = when (p) {
        1 -> "left"
        2 -> "right"
        else -> "unknown"
    }

    /** ATT application error codes from Control writes. */
    fun errorName(code: Int) = when (code) {
        0x80 -> "unknown opcode"
        0x81 -> "bad length"
        0x82 -> "bad parameter"
        0x83 -> "not allowed in this state"
        0x84 -> "sensor fault"
        else -> "GATT error 0x%02x".format(code)
    }

    private fun le(b: ByteArray) = ByteBuffer.wrap(b).order(ByteOrder.LITTLE_ENDIAN)
    private fun ByteBuffer.u8(i: Int) = get(i).toInt() and 0xFF
    private fun ByteBuffer.u16(i: Int) = getShort(i).toInt() and 0xFFFF
    private fun ByteBuffer.u32(i: Int) = getInt(i).toLong() and 0xFFFFFFFFL

    // ---- advertising --------------------------------------------------------

    /** Manufacturer data after the company id (Android's
     *  ScanRecord.getManufacturerSpecificData strips it). */
    data class Adv(val version: Int, val state: Int, val position: Int, val batteryMv: Int)

    fun parseAdv(afterCompanyId: ByteArray): Adv? {
        if (afterCompanyId.size < 5) return null
        val b = le(afterCompanyId)
        return Adv(b.u8(0), b.u8(1), b.u8(2), b.u16(3))
    }

    // ---- Info ---------------------------------------------------------------

    data class Info(
        val version: Int,
        val fwMajor: Int, val fwMinor: Int, val fwPatch: Int,
        val odrMask: Int, val accelRangeMask: Int, val gyroRangeMask: Int,
        val bufferCapacity: Long,
    ) {
        val firmware get() = "$fwMajor.$fwMinor.$fwPatch"
    }

    fun parseInfo(v: ByteArray): Info? {
        if (v.size < 12) return null
        val b = le(v)
        return Info(b.u8(0), b.u8(1), b.u8(2), b.u8(3), b.u16(4), b.u8(6), b.u8(7), b.u32(8))
    }

    // ---- Status -------------------------------------------------------------

    data class Status(
        val state: Int,
        val flags: Int,
        val batteryMv: Int,
        val sessionId: Int,
        val odrHz: Int,
        val accelRangeG: Int,
        val position: Int,
        val gyroRangeDps: Int,
        val samplesRecorded: Long,
        val samplesDropped: Long,
        val bufferFill: Long,
        val tagTimeUs: Long,
    )

    fun parseStatus(v: ByteArray): Status? {
        if (v.size < 32) return null
        val b = le(v)
        return Status(
            state = b.u8(0), flags = b.u8(1), batteryMv = b.u16(2), sessionId = b.u16(4),
            odrHz = b.u16(6), accelRangeG = b.u8(8), position = b.u8(9),
            gyroRangeDps = b.u16(10), samplesRecorded = b.u32(12),
            samplesDropped = b.u32(16), bufferFill = b.u32(20), tagTimeUs = b.getLong(24),
        )
    }

    // ---- Data ---------------------------------------------------------------

    /** Data packet header. [sampleIndex] is u32 (kept as Long). */
    data class DataHeader(val type: Int, val n: Int, val seq: Int, val sampleIndex: Long, val timestampUs: Long)

    fun parseDataHeader(v: ByteArray): DataHeader? {
        if (v.size < DATA_HEADER) return null
        val b = le(v)
        return DataHeader(b.u8(0), b.u8(1), b.u16(2), b.u32(4), b.getLong(8))
    }

    /** Sample [i] of an IMU6 packet as (ax, ay, az, gx, gy, gz) raw values. */
    fun imu6Sample(v: ByteArray, i: Int): IntArray {
        val b = le(v)
        val off = DATA_HEADER + i * IMU6_SAMPLE
        return IntArray(6) { b.getShort(off + 2 * it).toInt() }
    }

    // ---- Control ------------------------------------------------------------

    /** START with every field written out; 0 means the tag's default. */
    fun start(odrHz: Int = 200, accelRangeG: Int = 16, gyroRangeDps: Int = 2000): ByteArray =
        ByteBuffer.allocate(6).order(ByteOrder.LITTLE_ENDIAN)
            .put(OP_START.toByte())
            .putShort(odrHz.toShort())
            .put(accelRangeG.toByte())
            .putShort(gyroRangeDps.toShort())
            .array()

    fun stop() = byteArrayOf(OP_STOP.toByte())
    fun identify(seconds: Int) = byteArrayOf(OP_IDENTIFY.toByte(), seconds.toByte())
    fun setPosition(position: Int) = byteArrayOf(OP_SET_POSITION.toByte(), position.toByte())
    fun sleep() = byteArrayOf(OP_SLEEP.toByte())

    // ---- helpers ------------------------------------------------------------

    /** "01000100 3f000f1f 401f0000", the info_hex style of session.json. */
    fun hexGroups(v: ByteArray): String =
        v.toList().chunked(4).joinToString(" ") { g -> g.joinToString("") { "%02x".format(it) } }
}
