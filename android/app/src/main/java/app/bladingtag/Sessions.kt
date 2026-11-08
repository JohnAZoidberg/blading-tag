package app.bladingtag

import android.app.ActivityManager
import android.app.ApplicationExitInfo
import android.content.ContentValues
import android.content.Context
import android.net.Uri
import android.os.Environment
import android.provider.MediaStore
import org.json.JSONObject
import java.io.File
import java.text.SimpleDateFormat
import java.util.Locale
import java.util.TimeZone
import java.util.zip.ZipEntry
import java.util.zip.ZipOutputStream

/**
 * Recorded sessions on the phone. They live in the app's external files dir
 * (Android/data/app.bladingtag/files/sessions/), which `make pull` copies
 * over adb, and can be exported as a .zip to Downloads/blading-tag/ (the
 * folder zipped, as PROTOCOL.md §2 describes) for sharing.
 */
object Sessions {

    /** [json] is null if session.json is missing or unreadable (then it can't be edited). */
    class Summary(val dir: File, val json: SessionJson?, val durationS: Long?, val bytes: Long,
                  val tags: Int, val gpsFixes: Int)

    fun list(c: Context): List<Summary> =
        (RecorderService.sessionsDir(c).listFiles { f -> f.isDirectory && f.name.startsWith("session-") }
            ?: emptyArray())
            .sortedByDescending { it.name }
            .map { summarize(it) }

    /**
     * Sessions left without an end, because the process died with no chance
     * to save (reinstalled, killed by Android, force stopped), get an
     * end_reason afterwards from Android's record of why the process exited:
     * the first exit after the session started. Android keeps only the last
     * few, so old sessions may stay unexplained. [active] is being recorded.
     */
    fun explainUnfinished(c: Context, active: File?) {
        val open = (RecorderService.sessionsDir(c).listFiles { f -> f.isDirectory && f.name.startsWith("session-") }
            ?: emptyArray())
            .filter { it != active }
            .mapNotNull { dir ->
                val o = runCatching { JSONObject(File(dir, "session.json").readText()) }.getOrNull()
                    ?: return@mapNotNull null
                if (!o.isNull("end_elapsed_ns") || o.optString("end_reason").isNotEmpty()) return@mapNotNull null
                val start = runCatching { utc().parse(o.getString("start_utc"))!!.time }.getOrNull()
                    ?: return@mapNotNull null
                dir to start
            }
        if (open.isEmpty()) return
        val exits = c.getSystemService(ActivityManager::class.java)
            .getHistoricalProcessExitReasons(c.packageName, 0, 0)
            .sortedBy { it.timestamp }
        for ((dir, start) in open) {
            val exit = exits.firstOrNull { it.timestamp > start } ?: continue
            val (reason, detail) = exitReason(exit)
            runCatching { SessionJson.setEndReason(dir, reason, detail) }
        }
    }

    private fun utc() = SimpleDateFormat("yyyy-MM-dd'T'HH:mm:ss.SSS'Z'", Locale.US)
        .apply { timeZone = TimeZone.getTimeZone("UTC") }

    /** The [EndReason] and its detail for how the process ended. */
    private fun exitReason(e: ApplicationExitInfo): Pair<String, String?> = when (e.reason) {
        ApplicationExitInfo.REASON_CRASH -> EndReason.CRASH to null
        ApplicationExitInfo.REASON_CRASH_NATIVE -> EndReason.CRASH to "native"
        else -> EndReason.KILLED to when (e.reason) {
            ApplicationExitInfo.REASON_PACKAGE_UPDATED -> "app updated"
            ApplicationExitInfo.REASON_LOW_MEMORY -> "low memory"
            ApplicationExitInfo.REASON_USER_REQUESTED -> "force stopped"
            ApplicationExitInfo.REASON_ANR -> "not responding"
            ApplicationExitInfo.REASON_SIGNALED -> "signal ${e.status}"
            ApplicationExitInfo.REASON_EXCESSIVE_RESOURCE_USAGE -> "too much resource use"
            ApplicationExitInfo.REASON_PERMISSION_CHANGE -> "permission changed"
            ApplicationExitInfo.REASON_EXIT_SELF -> "exited"
            ApplicationExitInfo.REASON_FREEZER -> "frozen"
            else -> "reason ${e.reason}"
        }
    }

    private fun summarize(dir: File): Summary {
        val files = dir.listFiles().orEmpty()
        val json = SessionJson.read(dir)
        val start = json?.startElapsedNs
        val end = json?.endElapsedNs
        val gps = File(dir, "gps.csv").takeIf { it.exists() }
            ?.useLines { l -> l.count() - 1 }?.coerceAtLeast(0) ?: 0
        return Summary(
            dir = dir,
            json = json,
            durationS = if (start != null && end != null) (end - start) / 1_000_000_000 else null,
            bytes = files.sumOf { it.length() },
            tags = files.count { it.name.startsWith("tag-") },
            gpsFixes = gps,
        )
    }

    /** Zips [dir] into Downloads/blading-tag/<name>.zip and returns its content Uri. */
    fun exportZip(c: Context, dir: File): Uri {
        val values = ContentValues().apply {
            put(MediaStore.Downloads.DISPLAY_NAME, dir.name + ".zip")
            put(MediaStore.Downloads.MIME_TYPE, "application/zip")
            put(MediaStore.Downloads.RELATIVE_PATH, Environment.DIRECTORY_DOWNLOADS + "/blading-tag")
            put(MediaStore.Downloads.IS_PENDING, 1)
        }
        val resolver = c.contentResolver
        val uri = resolver.insert(MediaStore.Downloads.EXTERNAL_CONTENT_URI, values)
            ?: error("could not create the file in Downloads")
        try {
            resolver.openOutputStream(uri)!!.use { os ->
                ZipOutputStream(os.buffered()).use { zip ->
                    dir.listFiles().orEmpty().filter { it.isFile && !it.name.endsWith(".tmp") }
                        .sortedBy { it.name }
                        .forEach { f ->
                            zip.putNextEntry(ZipEntry("${dir.name}/${f.name}"))
                            f.inputStream().use { it.copyTo(zip) }
                            zip.closeEntry()
                        }
                }
            }
            resolver.update(uri, ContentValues().apply { put(MediaStore.Downloads.IS_PENDING, 0) }, null, null)
        } catch (t: Throwable) {
            resolver.delete(uri, null, null)
            throw t
        }
        return uri
    }
}
