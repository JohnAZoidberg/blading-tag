package app.bladingtag

import org.junit.Assert.assertEquals
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder

class SessionGpsTest {
    @get:Rule val folder = TemporaryFolder()

    @Test fun distanceUsesConsecutiveSpeedSamples() {
        assertEquals(5 to 33.0, summarize(2f, 4f, null, 30f, 30f))
    }

    @Test fun missingDistanceAndRecordedZeroAreDistinct() {
        assertEquals(0 to null, summarize())
        assertEquals(1 to null, summarize(2f))
        assertEquals(2 to 0.0, summarize(0f, 0f))
    }

    private fun summarize(vararg speeds: Float?): Pair<Int, Double?> {
        val file = folder.newFile()
        val log = GpsLog(file)
        speeds.forEachIndexed { index, speed ->
            log.append(GpsLog.Fix(index * 1_000_000_000L, 0, 0.0, 0.0, null, speed, null, null, null))
        }
        log.close()
        return summarizeGps(file)
    }
}
