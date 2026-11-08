package app.bladingtag

import kotlin.math.sqrt

/**
 * One tag taking part in a recording. Subclasses do the transport ([GattTag]
 * for a real tag, [SimulatedTag] for testing without one); this base class
 * hands every Status/Data payload to the [Listener] verbatim and keeps the
 * live counters the UI shows.
 *
 * Counters are written from the link's own thread and read from the UI
 * thread, hence @Volatile; a slightly stale read is fine for a display.
 */
abstract class TagLink(val address: String, protected val listener: Listener) {

    interface Listener {
        fun onInfo(link: TagLink, raw: ByteArray)
        /** [kind] is [TagLog.KIND_DATA] or [TagLog.KIND_STATUS]. */
        fun onRecord(link: TagLink, kind: Int, elapsedNs: Long, payload: ByteArray)
        fun onStatus(link: TagLink, status: Protocol.Status) {}
        /** The tag went from RECORDING to READY on its own while connected:
         *  someone pressed its button. The link stops reconnecting/restarting;
         *  call [stop] to close it. */
        fun onTagStopped(link: TagLink) {}
    }

    /** Connect (and keep reconnecting) and get the tag recording. */
    abstract fun start()

    /** Ask the tag to stop, wait for the rest of its data, then disconnect.
     *  [done] runs once, on any thread. */
    abstract fun stop(done: () -> Unit)

    /** Send a Control command while connected (IDENTIFY, ...). */
    abstract fun command(bytes: ByteArray)

    @Volatile var connected = false; protected set
    @Volatile var info: Protocol.Info? = null; protected set
    @Volatile var status: Protocol.Status? = null; protected set
    @Volatile var mtu = 23; protected set
    @Volatile var error: String? = null
    @Volatile var reconnects = 0; protected set
    /** Times the tag was found READY mid-session and had to be started again. */
    @Volatile var restarts = 0; protected set

    @Volatile var packets = 0L; private set
    @Volatile var samples = 0L; private set
    /** Packets missing according to seq (should only happen across a disconnect). */
    @Volatile var seqGaps = 0L; private set
    /** Samples missing according to sample_index (tag buffer overflow). */
    @Volatile var missingSamples = 0L; private set
    /** Last accel magnitude in g, as a sanity check (≈1 when still). */
    @Volatile var accelG = Double.NaN; private set

    private var lastSeq = -1
    private var nextIndex = -1L
    private var firstIndex = -1L
    private var firstTsUs = 0L
    @Volatile private var lastIndex = -1L
    @Volatile private var lastTsUs = 0L
    private var lastSessionId = -1

    /** Sample rate measured from the tag's own timestamps, Hz. */
    val measuredRate: Double
        get() {
            val di = lastIndex - firstIndex
            val dt = lastTsUs - firstTsUs
            return if (firstIndex < 0 || di < 200 || dt <= 0) Double.NaN else di * 1e6 / dt
        }

    /** Call when a new tag-side session starts, so gaps aren't counted across it. */
    protected fun resetSequence() {
        lastSeq = -1
        nextIndex = -1
        firstIndex = -1
        lastIndex = -1
    }

    protected fun handleInfo(raw: ByteArray) {
        info = Protocol.parseInfo(raw)
        listener.onInfo(this, raw)
    }

    protected fun handleStatus(elapsedNs: Long, raw: ByteArray) {
        listener.onRecord(this, TagLog.KIND_STATUS, elapsedNs, raw)
        val s = Protocol.parseStatus(raw) ?: return
        if (s.sessionId != lastSessionId) {
            if (lastSessionId != -1) resetSequence()
            lastSessionId = s.sessionId
        }
        status = s
        listener.onStatus(this, s)
    }

    protected fun handleData(elapsedNs: Long, raw: ByteArray) {
        listener.onRecord(this, TagLog.KIND_DATA, elapsedNs, raw)
        val h = Protocol.parseDataHeader(raw) ?: return
        packets++
        // After a reconnect the tag sends unacknowledged packets again (PROTOCOL.md 1.9):
        // same or earlier seq, samples already seen. Those aren't gaps.
        val dSeq = (h.seq - lastSeq) and 0xFFFF
        if (lastSeq < 0 || dSeq in 1 until 0x8000) {
            if (lastSeq >= 0 && dSeq != 1) seqGaps++
            lastSeq = h.seq
        }
        if (h.type != Protocol.PACKET_IMU6) return
        val n = minOf(h.n, (raw.size - Protocol.DATA_HEADER) / Protocol.IMU6_SAMPLE)
        if (n <= 0) return
        val end = h.sampleIndex + n
        if (nextIndex >= 0 && end <= nextIndex) return
        samples += if (nextIndex >= 0) end - maxOf(h.sampleIndex, nextIndex) else n.toLong()
        if (nextIndex >= 0 && h.sampleIndex > nextIndex) missingSamples += h.sampleIndex - nextIndex
        nextIndex = end
        if (firstIndex < 0) {
            firstIndex = h.sampleIndex
            firstTsUs = h.timestampUs
        }
        lastIndex = h.sampleIndex
        lastTsUs = h.timestampUs
        val range = status?.accelRangeG?.takeIf { it > 0 } ?: 16
        val s = Protocol.imu6Sample(raw, n - 1)
        val ax = s[0] * range / 32768.0
        val ay = s[1] * range / 32768.0
        val az = s[2] * range / 32768.0
        accelG = sqrt(ax * ax + ay * ay + az * az)
    }
}
