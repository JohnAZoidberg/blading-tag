package app.bladingtag

import org.junit.Assert.*
import org.junit.Test
import org.json.JSONObject
import java.nio.file.Files

class HeartRateTest {
    private fun hex(s: String) = s.split(" ").map { it.toInt(16).toByte() }.toByteArray()
    @Test fun liveValuesExcludeUnavailableMeasurements() {
        val valid = HeartRate.decode(hex("06 48"))
        assertEquals(72f, HeartRate.liveBpm(valid, 0), 0f)
        assertEquals(72f, HeartRate.liveBpm(HeartRate.decode(hex("00 48")), 1), 0f)
        for (m in listOf(null, HeartRate.decode(hex("04 48")), HeartRate.decode(hex("06 00"))))
            assertTrue(HeartRate.liveBpm(m, 0).isNaN())
        assertTrue(HeartRate.liveBpm(valid, 5_000_000_000L).isNaN())
        assertEquals(72f, HeartRate.liveBpm(valid, 4_999_999_999L), 0f)
    }
    @Test fun trendRetainsHeartRateAndGapsAcrossGrowth() {
        val trend = Trend()
        repeat(1100) { i -> trend.add(i.toFloat(), Float.NaN, Float.NaN,
            if (i == 10) Float.NaN else 72f) }
        assertEquals(1100, trend.size)
        assertTrue(trend.heartRateBpm[10].isNaN())
        assertEquals(72f, trend.heartRateBpm[1099], 0f)
        assertEquals(1099f, trend.t[1099], 0f)
        assertEquals(0, Trend().size)
    }
    @Test fun giantCapturedPackets() {
        val m = HeartRate.decode(hex("16 47 56 03 54 03"))
        assertEquals(71, m.bpm)
        assertEquals(true, m.contactDetected)
        assertEquals(listOf(854, 852), m.rrTicks)
        assertNull(m.energyKj)
    }
    @Test fun optionalFieldsAndZero() {
        val m = HeartRate.decode(hex("19 2c 01 07 00 00 04"))
        assertEquals(300, m.bpm)
        assertEquals(7, m.energyKj)
        assertNull(m.contactDetected)
        assertEquals(listOf(1024), m.rrTicks)
        assertEquals(0, HeartRate.decode(hex("06 00")).bpm)
        assertEquals(false, HeartRate.decode(hex("04 48")).contactDetected)
    }
    @Test fun malformedPacketsRejected() {
        for (raw in listOf(byteArrayOf(), hex("00"), hex("01 40"), hex("10 40"),
                           hex("10 40 01"), hex("00 40 01"), hex("08 40 01"))) {
            assertTrue(runCatching { HeartRate.decode(raw) }.isFailure)
        }
    }
    @Test fun rawLoggingAndMetadataSurviveEdits() {
        val dir = Files.createTempDirectory("hrm-test").toFile()
        try {
            val f = dir.resolve("heartrate.csv")
            val log = HeartRateLog(f)
            log.append(9007199254740993L, hex("16 47 56 03 54 03"))
            log.append(9007199254740994L, hex("10 40 01"))
            log.close()
            log.append(0, byteArrayOf())
            val rows = f.readLines()
            assertEquals(3, rows.size)
            assertEquals("9007199254740993,71,true,true,,\"[854,852]\",164756035403", rows[1])
            assertEquals("9007199254740994,,,,,,104001", rows[2])
            val meta = SessionMeta("test", 0, 0, emptyList())
            meta.heartRateEnabled = true
            meta.heartRate = HeartRateMeta("AA:BB:CC:DD:EE:FF", "Giant HRM")
            writeSessionJson(dir, meta)
            SessionJson.edit(dir, "test", emptyMap())
            val json = JSONObject(dir.resolve("session.json").readText())
            assertEquals(1, json.getInt("format_version"))
            assertEquals("heartrate.csv", json.getJSONObject("heart_rate").getString("file"))
        } finally { dir.deleteRecursively() }
    }
}
