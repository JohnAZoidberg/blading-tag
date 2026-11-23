package app.bladingtag

import java.io.File
import java.nio.ByteBuffer
import java.nio.ByteOrder

internal class RecordedSession(summary: Sessions.Summary) {
    class Trace(val t: FloatArray, val channels: Array<FloatArray>)
    data class Point(val lat: Double, val lon: Double)
    private class Samples(private val count: Int) {
        private var times = FloatArray(1024)
        private var values = Array(count) { FloatArray(times.size) }
        private var size = 0
        fun breakAfter(t: Float, timeout: Float) {
            if (size > 0 && t - times[size - 1] > timeout) {
                add(times[size - 1] + timeout, *FloatArray(count) { Float.NaN })
            }
        }

        fun add(t: Float, vararg row: Float) {
            if (size > 0 && t < times[size - 1]) return
            if (size == times.size) {
                times = times.copyOf(size * 2)
                values = Array(count) { values[it].copyOf(size * 2) }
            }
            times[size] = t
            for (i in 0 until count) values[i][size] = row[i]
            size++
        }
        fun trace() = Trace(times.copyOf(size), Array(count) { values[it].copyOf(size) })
    }

    val totals = Trend()
    val route = mutableListOf<Point>()
    val gps: Trace
    val heartRate: Trace
    val motion: List<Pair<String, Trace>>
    val cadence: Trace
    val unit = RateUnit.of(summary.json?.activity ?: ActivityKind.UNKNOWN)
    val distancePerCycle: Float
    val duration: Float
    val end: Float

    init {
        val gpsFile = File(summary.dir, "gps.csv")
        val hrFile = File(summary.dir, "heartrate.csv")
        val tagFiles = summary.tagFiles.sorted().map { File(summary.dir, it) }
        fun firstCsv(file: File) = if (file.isFile) file.useLines {
            it.drop(1).firstOrNull()?.substringBefore(',')?.toLongOrNull()
        } else null
        val origin = summary.json?.startElapsedNs ?: buildList {
            firstCsv(gpsFile)?.let { add(it) }
            firstCsv(hrFile)?.let { add(it) }
            for (file in tagFiles) records(file) { ns, _, _ -> add(ns); false }
        }.minOrNull() ?: 0L
        fun seconds(ns: Long) = (ns - origin) / 1e9f
        val gpsSamples = Samples(2)
        if (gpsFile.isFile) gpsFile.useLines { lines ->
            for (line in lines.drop(1)) {
                val f = line.split(',', limit = 7)
                val ns = f[0].toLongOrNull() ?: continue
                val lat = f.getOrNull(2)?.toDoubleOrNull()
                val lon = f.getOrNull(3)?.toDoubleOrNull()
                if (lat != null && lon != null && lat in -90.0..90.0 && lon in -180.0..180.0) {
                    route.add(Point(lat, lon))
                }
                val speed = f.getOrNull(5)?.toFloatOrNull()?.takeIf { it.isFinite() && it >= 0 }?.times(3.6f)
                    ?: Float.NaN
                val altitude = f.getOrNull(4)?.toFloatOrNull()?.takeIf { it.isFinite() } ?: Float.NaN
                totals.observeSpeed(speed)
                totals.elevation.observe(altitude)
                gpsSamples.breakAfter(seconds(ns), 5f)
                gpsSamples.add(seconds(ns), speed, altitude)
            }
        }
        gps = gpsSamples.trace()
        val hrSamples = Samples(1)
        if (hrFile.isFile) hrFile.useLines { lines ->
            for (line in lines.drop(1)) {
                val f = line.split(',', limit = 5)
                val ns = f[0].toLongOrNull() ?: continue
                val bpm = f.getOrNull(1)?.toFloatOrNull()?.takeIf {
                    it.isFinite() && it > 0 && (f.getOrNull(2) != "true" || f.getOrNull(3) == "true")
                } ?: Float.NaN
                totals.observeHeartRate(bpm)
                hrSamples.breakAfter(seconds(ns), 5f)
                hrSamples.add(seconds(ns), bpm)
            }
        }
        heartRate = hrSamples.trace()
        val rates = sortedMapOf<Int, MutableList<Float>>()
        motion = tagFiles.map { file ->
            val samples = Samples(6)
            val signal = LiveSignal()
            var status: Protocol.Status? = null
            var nextIndex = -1L
            var previousUs = -1L
            var anchorUs = 0L
            var anchorTime = 0f
            var previousTime = Float.NEGATIVE_INFINITY
            var tick = -1
            records(file) { ns, kind, bytes ->
                if (kind == TagLog.KIND_STATUS) status = Protocol.parseStatus(bytes) ?: status
                if (kind == TagLog.KIND_DATA) {
                    val packet = Protocol.parseDataHeader(bytes)
                    if (packet?.type == Protocol.PACKET_IMU6) {
                        val odr = status?.odrHz?.takeIf { it > 0 } ?: 200
                        val accel = (status?.accelRangeG?.takeIf { it > 0 } ?: 16) / 32768f
                        val gyro = (status?.gyroRangeDps?.takeIf { it > 0 } ?: 2000) / 32768f
                        val count = minOf(packet.n, (bytes.size - Protocol.DATA_HEADER) / Protocol.IMU6_SAMPLE)
                        if (previousUs < 0 || packet.timestampUs < previousUs && packet.sampleIndex == 0L) {
                            nextIndex = -1
                            anchorUs = packet.timestampUs
                            anchorTime = maxOf(seconds(ns) - (count - 1) / odr.toFloat(), previousTime + 1f / odr, 0f)
                        }
                        val first = maxOf(0L, nextIndex - packet.sampleIndex).coerceAtMost(count.toLong()).toInt()
                        val start = anchorTime + (packet.timestampUs - anchorUs) / 1e6f
                        if (nextIndex >= 0 && packet.sampleIndex > nextIndex || nextIndex < 0 && previousTime.isFinite()) {
                            signal.gap()
                            samples.add(previousTime, *FloatArray(6) { Float.NaN })
                        }
                        for (i in first until count) {
                            val t = maxOf(previousTime, start + i.toFloat() / odr)
                            val raw = Protocol.imu6Sample(bytes, i)
                            samples.add(t, *FloatArray(6) { raw[it] * if (it < 3) accel else gyro })
                            signal.add(raw, odr, (accel * 32768).toInt(), (gyro * 32768).toInt())
                            previousTime = t
                        }
                        if (count > first) {
                            previousUs = packet.timestampUs
                            nextIndex = packet.sampleIndex + count
                            val second = previousTime.toInt()
                            if (second > tick) {
                                tick = second
                                val values = rates.getOrPut(second) { mutableListOf() }
                                signal.cadence()?.takeIf { it.valid }?.let { values.add(it.cyclesPerMin) }
                            }
                        }
                    }
                }
                true
            }
            val tag = summary.json?.tags?.firstOrNull { TagLog.fileName(it.address) == file.name }
            val name = if (tag?.simulated == true) "Simulated" else tag?.let {
                Protocol.positionLabel(it.position).takeIf(String::isNotBlank)
            } ?: file.nameWithoutExtension.takeLast(5)
            name to samples.trace()
        }
        val cadenceSamples = Samples(1)
        var fix = -1
        var metres = 0.0
        var repetitions = 0.0
        for ((second, values) in rates) {
            val rate = if (values.isEmpty()) Float.NaN else unit.rate(values.average().toFloat())
            cadenceSamples.add(second.toFloat(), rate)
            while (fix + 1 < gps.t.size && gps.t[fix + 1] <= second) fix++
            if (rate > 0 && fix >= 0 && second - gps.t[fix] < 5f) {
                val speed = gps.channels[0][fix]
                if (speed >= LivePanel.MIN_PER_CYCLE_KMH) {
                    metres += speed / 3.6
                    repetitions += rate / 60.0
                }
            }
        }
        cadence = cadenceSamples.trace()
        distancePerCycle = (metres / repetitions).toFloat()
        end = maxOf(0f, summary.json?.endElapsedNs?.let(::seconds) ?: 0f,
            gps.t.lastOrNull() ?: 0f, heartRate.t.lastOrNull() ?: 0f,
            motion.maxOfOrNull { it.second.t.lastOrNull() ?: 0f } ?: 0f)
        duration = summary.json?.endElapsedNs?.let(::seconds)?.coerceAtLeast(0f) ?: end
    }

    private fun records(file: File, read: (Long, Int, ByteArray) -> Boolean) {
        file.inputStream().buffered().use { input ->
            val header = ByteArray(TagLog.HEADER)
            val buffer = ByteBuffer.wrap(header).order(ByteOrder.LITTLE_ENDIAN)
            while (input.readNBytes(header, 0, header.size) == header.size) {
                val len = buffer.getShort(9).toInt() and 0xffff
                val bytes = input.readNBytes(len)
                if (bytes.size != len || !read(buffer.getLong(0), header[8].toInt(), bytes)) break
            }
        }
    }
}
