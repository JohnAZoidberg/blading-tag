package app.bladingtag

import android.content.ContentValues
import android.content.Context
import android.net.Uri
import android.os.Environment
import android.provider.MediaStore
import java.io.File
import java.util.zip.ZipEntry
import java.util.zip.ZipOutputStream

/**
 * Recorded sessions on the phone. They live in the app's external files dir
 * (Android/data/app.bladingtag/files/sessions/), which `make pull` copies
 * over adb, and can be exported as a .zip to Downloads/blading-tag/ (the
 * folder zipped, as PROTOCOL.md §2 describes) for sharing.
 */
object Sessions {

    class Summary(val dir: File, val durationS: Long?, val bytes: Long, val tags: Int, val gpsFixes: Int)

    fun list(c: Context): List<Summary> =
        (RecorderService.sessionsDir(c).listFiles { f -> f.isDirectory && f.name.startsWith("session-") }
            ?: emptyArray())
            .sortedByDescending { it.name }
            .map { summarize(it) }

    private fun summarize(dir: File): Summary {
        val files = dir.listFiles().orEmpty()
        val json = File(dir, "session.json").takeIf { it.exists() }?.readText().orEmpty()
        fun num(key: String) = Regex("\"$key\":\\s*(\\d+)").find(json)?.groupValues?.get(1)?.toLong()
        val start = num("start_elapsed_ns")
        val end = num("end_elapsed_ns")
        val gps = File(dir, "gps.csv").takeIf { it.exists() }
            ?.useLines { l -> l.count() - 1 }?.coerceAtLeast(0) ?: 0
        return Summary(
            dir = dir,
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
