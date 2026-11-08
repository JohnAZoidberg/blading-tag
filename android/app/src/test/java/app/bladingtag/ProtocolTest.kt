package app.bladingtag

import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File
import java.nio.file.Files

/** The hex vectors from PROTOCOL.md. */
class ProtocolTest {

    private fun hex(s: String) = s.replace(Regex("\\s"), "").chunked(2).map { it.toInt(16).toByte() }.toByteArray()

    @Test fun uuids() {
        assertEquals("b1ad0001-0e83-479a-bb08-19dab75f76db", Protocol.SERVICE.toString())
        assertEquals("b1ad0005-0e83-479a-bb08-19dab75f76db", Protocol.DATA.toString())
    }

    @Test fun startDefaults() {
        assertArrayEquals(hex("01 c8 00 10 d0 07"), Protocol.start())
    }

    @Test fun status() {
        val s = Protocol.parseStatus(hex(
            "01 00 86 0b 03 00 c8 00 10 00 d0 07 70 17 00 00 00 00 00 00 26 00 00 00 78 0a e3 05 00 00 00 00"))!!
        assertEquals(Protocol.STATE_RECORDING, s.state)
        assertEquals(0, s.flags)
        assertEquals(2950, s.batteryMv)
        assertEquals(3, s.sessionId)
        assertEquals(200, s.odrHz)
        assertEquals(16, s.accelRangeG)
        assertEquals(0, s.position)
        assertEquals(2000, s.gyroRangeDps)
        assertEquals(6000L, s.samplesRecorded)
        assertEquals(0L, s.samplesDropped)
        assertEquals(38L, s.bufferFill)
        assertEquals(98_765_432L, s.tagTimeUs)
    }

    @Test fun statusTooShortOrLonger() {
        assertNull(Protocol.parseStatus(ByteArray(31)))
        // Compatible changes append fields; they must be ignored.
        assertEquals(0, Protocol.parseStatus(ByteArray(40))!!.state)
    }

    private val dataExample = hex("""
        01 02 05 00 e8 03 00 00 4e 61 bc 00 00 00 00 00
        00 00 00 00 00 08 00 00 00 00 00 00
        64 00 9c ff 00 08 10 00 f0 ff 00 00""")

    @Test fun data() {
        val h = Protocol.parseDataHeader(dataExample)!!
        assertEquals(Protocol.PACKET_IMU6, h.type)
        assertEquals(2, h.n)
        assertEquals(5, h.seq)
        assertEquals(1000L, h.sampleIndex)
        assertEquals(12_345_678L, h.timestampUs)
        assertArrayEquals(intArrayOf(0, 0, 2048, 0, 0, 0), Protocol.imu6Sample(dataExample, 0))
        assertArrayEquals(intArrayOf(100, -100, 2048, 16, -16, 0), Protocol.imu6Sample(dataExample, 1))
    }

    @Test fun info() {
        val raw = hex("01000100 3f000f1f 401f0000")
        val i = Protocol.parseInfo(raw)!!
        assertEquals(1, i.version)
        assertEquals("0.1.0", i.firmware)
        assertEquals(0x3f, i.odrMask)
        assertEquals(0x0f, i.accelRangeMask)
        assertEquals(0x1f, i.gyroRangeMask)
        assertEquals(8000L, i.bufferCapacity)
        assertEquals("01000100 3f000f1f 401f0000", Protocol.hexGroups(raw))
    }

    @Test fun adv() {
        // After the 0xFFFF company id: version 1, READY, left, 2950 mV.
        val a = Protocol.parseAdv(hex("01 00 01 86 0b"))!!
        assertEquals(Protocol.Adv(1, Protocol.STATE_READY, 1, 2950), a)
    }

    @Test fun tagLogRecord() {
        val dir = Files.createTempDirectory("bt").toFile()
        val f = File(dir, TagLog.fileName("C0:BB:4A:A7:0D:57"))
        assertEquals("tag-C0BB4AA70D57.bin", f.name)
        TagLog(f).apply { append(123456789012L, TagLog.KIND_DATA, dataExample); close() }
        val bytes = f.readBytes()
        assertArrayEquals(hex("14 1a 99 be 1c 00 00 00 01 28 00"), bytes.copyOfRange(0, 11))
        assertArrayEquals(dataExample, bytes.copyOfRange(11, bytes.size))
        dir.deleteRecursively()
    }

    @Test fun sessionJson() {
        val t = TagMeta("C0:BB:4A:A7:0D:57").apply {
            position = "left_foot"; infoHex = "01000100 3f000f1f 401f0000"
            sessionId = 3; odrHz = 200; accelRangeG = 16; gyroRangeDps = 2000
        }
        val m = SessionMeta("0.1.0", 1_794_061_530_123L, 123456789012L, listOf(t))
        m.endElapsedNs = 130656789012L
        val j = m.toJson()
        assertTrue(j, j.contains("\"start_utc\": \"2026-11-07T14:25:30.123Z\""))
        assertTrue(j, j.contains("\"end_elapsed_ns\": 130656789012"))
        assertTrue(j, j.contains("\"file\": \"tag-C0BB4AA70D57.bin\""))
        assertTrue(j, j.contains("\"session_id\": 3"))
        assertEquals("session-20261107-142530", SessionMeta.folderName(1_794_061_530_123L))
        assertEquals("\"a\\\"b\\n\"", SessionMeta.q("a\"b\n"))
    }

    @Test fun editSessionJson() {
        val dir = Files.createTempDirectory("session").toFile()
        val left = TagMeta("C0:BB:4A:A7:0D:57").apply { position = "left_foot"; sessionId = 3; odrHz = 200 }
        val other = TagMeta("C0:BB:4A:A7:0D:58").apply { position = "right" } // from before body locations
        val m = SessionMeta("0.1.0", 1_794_061_530_123L, 123456789012L, listOf(left, other))
        writeSessionJson(dir, m)

        var j = SessionJson.read(dir)!!
        assertEquals("", j.notes)
        assertEquals(123456789012L, j.startElapsedNs)
        assertNull(j.endElapsedNs)
        assertEquals(listOf("left_foot", "right_foot"), j.tags.map { it.position })

        // Only the named tag changes; other fields survive the rewrite.
        SessionJson.edit(dir, "crossovers \"fast\"\nline 2", mapOf("C0:BB:4A:A7:0D:57" to "backpack"))
        j = SessionJson.read(dir)!!
        assertEquals("crossovers \"fast\"\nline 2", j.notes)
        assertEquals(listOf("backpack", "right_foot"), j.tags.map { it.position })
        assertEquals(123456789012L, j.startElapsedNs)
        val text = File(dir, "session.json").readText()
        assertTrue(text, text.contains("\"session_id\": 3"))
        assertTrue(text, text.contains("\"start_utc\": \"2026-11-07T14:25:30.123Z\""))
        assertTrue(text, text.contains("\"file\": \"tag-C0BB4AA70D57.bin\""))
        assertNull(SessionJson.read(File(dir, "missing")))
        dir.deleteRecursively()
    }

    @Test fun simulatedSessions() {
        val real = TagMeta("C0:BB:4A:A7:0D:57")
        val sim = TagMeta(SimulatedTag.ADDRESS).apply { simulated = true }
        val m = SessionMeta("0.1.0", 1_794_061_530_123L, 1L, listOf(real, sim))
        assertTrue(m.toJson().contains("\"simulated\": true"))
        assertTrue(m.toJson().contains("\"simulated\": false"))
        assertTrue(sim.copy().simulated)
        val j = SessionJson.parse(m.toJson())
        assertEquals(listOf(false, true), j.tags.map { it.simulated })
        assertTrue(j.simulated)
        assertFalse(SessionJson.parse(SessionMeta("0.1.0", 0L, 1L, listOf(real)).toJson()).simulated)

        // From before the field: recognised by the simulators' addresses.
        fun old(address: String) = SessionJson.parse(
            "{\"start_elapsed_ns\": 1, \"tags\": [{\"address\": \"$address\"}], \"notes\": \"\"}")
        assertTrue(old("02:00:00:00:00:01").simulated)
        assertTrue(old("FA:KE:00:00:00:01").simulated)
        assertFalse(old("C0:BB:4A:A7:0D:57").simulated)
    }

    @Test fun bodyLocations() {
        Protocol.BODY_LOCATIONS.forEachIndexed { i, l -> assertEquals(i, l.value) }
        assertEquals("backpack", Protocol.positionName(7))
        assertEquals("unknown", Protocol.positionName(8))
        assertEquals("Left foot", Protocol.positionLabel("left"))
        assertEquals("Right pants pocket", Protocol.positionLabel("right_pocket"))
    }

    @Test fun closedLogRefusesAppend() {
        val dir = Files.createTempDirectory("log").toFile()
        val log = TagLog(File(dir, "t.bin"))
        assertTrue(log.append(1, TagLog.KIND_DATA, byteArrayOf(1)))
        log.close()
        assertTrue(!log.append(2, TagLog.KIND_DATA, byteArrayOf(2)))
        assertEquals(12L, File(dir, "t.bin").length())
        dir.deleteRecursively()
    }
}
