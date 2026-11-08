package app.bladingtag

import android.Manifest
import android.annotation.SuppressLint
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.content.pm.ServiceInfo
import android.location.Location
import android.location.LocationListener
import android.location.LocationManager
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.os.PowerManager
import android.os.SystemClock
import android.util.Log
import java.io.File

/**
 * Foreground service that owns a recording: the tag links, GPS and the
 * session files. It keeps running with the screen off and the app in the
 * background (and holds a partial wake lock so BLE callbacks and file
 * writes aren't deferred during a 3 h skate).
 *
 * The activity reads the live state from [current]; there's only ever one
 * recording, in this process, so no binder is needed.
 *
 * [split] ends the session and starts the next one without touching the tag
 * links: the tags keep recording and only the files change, so no sample is
 * lost at the cut and there's no reconnect.
 */
class RecorderService : Service(), TagLink.Listener {

    /** Live state of the running recording's current session, for the UI. A split replaces it. */
    class Recording(
        val dir: File,
        val meta: SessionMeta,
        val links: List<TagLink>,
        val logs: Map<String, TagLog>,
        val gps: GpsLog?,
    ) {
        val startElapsedNs get() = meta.startElapsedNs
        @Volatile var lastFix: Location? = null
        @Volatile var stopping = false
    }

    private val main = Handler(Looper.getMainLooper())
    private var wakeLock: PowerManager.WakeLock? = null
    private var locationListener: LocationListener? = null

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_START -> if (current == null) {
                begin(intent.getStringArrayExtra(EXTRA_ADDRESSES)?.toList().orEmpty(),
                    intent.getBooleanExtra(EXTRA_GPS, true))
            }
            ACTION_STOP -> end()
            ACTION_SPLIT -> split()
        }
        return START_NOT_STICKY
    }

    /** [wantGps]: the user's choice; GPS also needs the location permission. */
    private fun begin(addresses: List<String>, wantGps: Boolean) {
        val hasLocation = wantGps && granted(Manifest.permission.ACCESS_FINE_LOCATION)
        var types = ServiceInfo.FOREGROUND_SERVICE_TYPE_CONNECTED_DEVICE
        if (hasLocation) types = types or ServiceInfo.FOREGROUND_SERVICE_TYPE_LOCATION
        startForeground(NOTIFICATION_ID, notification("Starting…"), types)

        val startUtc = System.currentTimeMillis()
        val startElapsed = SystemClock.elapsedRealtimeNanos()
        val dir = newSessionDir(startUtc)
        val meta = SessionMeta(BuildConfig.VERSION_NAME, startUtc, startElapsed,
            addresses.map { TagMeta(it) })
        writeSessionJson(dir, meta)

        val logs = addresses.associateWith { TagLog(File(dir, TagLog.fileName(it))) }
        val links = addresses.map { addr ->
            if (addr == SimulatedTag.ADDRESS) SimulatedTag(this) else GattTag(this, addr, this)
        }
        val gps = if (hasLocation) GpsLog(File(dir, "gps.csv")) else null
        val rec = Recording(dir, meta, links, logs, gps)
        current = rec

        wakeLock = getSystemService(PowerManager::class.java)
            .newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "bladingtag:recording")
            .apply { acquire(12 * 3600 * 1000L) }
        if (gps != null) startGps()
        links.forEach { it.start() }
        Log.i(TAG, "recording to $dir with ${addresses.size} tag(s), gps=${gps != null}")
        main.postDelayed(tick, TICK_MS)
    }

    /** A new folder; "-2", "-3"... if a split comes within the same second. */
    private fun newSessionDir(startUtc: Long): File {
        val name = SessionMeta.folderName(startUtc)
        var dir = File(sessionsDir(this), name)
        var n = 2
        while (dir.exists()) dir = File(sessionsDir(this), "$name-${n++}")
        return dir.apply { mkdirs() }
    }

    /** Fixes go to whichever session is current (on the main thread, like [split]). */
    @SuppressLint("MissingPermission") // checked in begin()
    private fun startGps() {
        val lm = getSystemService(LocationManager::class.java)
        val l = LocationListener { loc ->
            val rec = current ?: return@LocationListener
            rec.lastFix = loc
            rec.gps?.append(GpsLog.Fix(
                elapsedNs = loc.elapsedRealtimeNanos,
                utcMs = loc.time,
                lat = loc.latitude, lon = loc.longitude,
                altM = if (loc.hasAltitude()) loc.altitude else null,
                speedMps = if (loc.hasSpeed()) loc.speed else null,
                bearingDeg = if (loc.hasBearing()) loc.bearing else null,
                hAccM = if (loc.hasAccuracy()) loc.accuracy else null,
                speedAccMps = if (loc.hasSpeedAccuracy()) loc.speedAccuracyMetersPerSecond else null,
            ))
        }
        locationListener = l
        lm.requestLocationUpdates(LocationManager.GPS_PROVIDER, 1000L, 0f, l, Looper.getMainLooper())
    }

    /** Flush files and refresh the notification every few seconds. */
    private val tick = object : Runnable {
        override fun run() {
            val rec = current ?: return
            rec.logs.values.forEach { it.flush() }
            rec.gps?.flush()
            getSystemService(NotificationManager::class.java)
                .notify(NOTIFICATION_ID, notification(summary(rec)))
            main.postDelayed(this, TICK_MS)
        }
    }

    /**
     * Saves the current session, ending it now, and continues the recording
     * in a new one with the same tags and GPS setting.
     */
    private fun split() {
        val old = current ?: return
        if (old.stopping) return
        val startUtc = System.currentTimeMillis()
        val startElapsed = SystemClock.elapsedRealtimeNanos()
        val dir = newSessionDir(startUtc)
        val logs = old.logs.keys.associateWith { TagLog(File(dir, TagLog.fileName(it))) }
        val gps = old.gps?.let { GpsLog(File(dir, "gps.csv")) }
        // Under the old meta's lock, so a concurrent updateMeta() either lands
        // before the copy or sees the new session and applies there.
        synchronized(old.meta) {
            val meta = SessionMeta(BuildConfig.VERSION_NAME, startUtc, startElapsed,
                old.meta.tags.map { it.copy() })
            writeSessionJson(dir, meta)
            current = Recording(dir, meta, old.links, logs, gps).apply { lastFix = old.lastFix }
        }
        // A packet being appended right now retries into the new log (onRecord).
        save(old, startElapsed)
        Log.i(TAG, "split: saved ${old.dir}, now recording to $dir")
    }

    /** Closes a session's files and records its end. */
    private fun save(rec: Recording, endElapsedNs: Long) {
        rec.logs.values.forEach { it.close() }
        rec.gps?.close()
        synchronized(rec.meta) {
            rec.meta.endElapsedNs = endElapsedNs
            writeSessionJson(rec.dir, rec.meta)
        }
    }

    private fun end() {
        val rec = current
        if (rec == null) { stopSelf(); return }
        if (rec.stopping) return
        rec.stopping = true
        getSystemService(NotificationManager::class.java)
            .notify(NOTIFICATION_ID, notification("Stopping…"))
        locationListener?.let { getSystemService(LocationManager::class.java).removeUpdates(it) }
        locationListener = null

        var pending = rec.links.size
        val finish = {
            main.removeCallbacks(tick)
            save(rec, SystemClock.elapsedRealtimeNanos())
            wakeLock?.takeIf { it.isHeld }?.release()
            current = null
            Log.i(TAG, "recording saved to ${rec.dir}")
            stopForeground(STOP_FOREGROUND_REMOVE)
            stopSelf()
        }
        if (pending == 0) { finish(); return }
        rec.links.forEach { link ->
            link.stop { main.post { if (--pending == 0) finish() } }
        }
    }

    override fun onDestroy() {
        // Killed without a stop (e.g. swiped away on some OEM ROMs): keep what we have.
        current?.let { rec ->
            rec.links.forEach { it.stop {} }
            save(rec, SystemClock.elapsedRealtimeNanos())
            current = null
        }
        wakeLock?.takeIf { it.isHeld }?.release()
        main.removeCallbacksAndMessages(null)
        super.onDestroy()
    }

    // ---- TagLink.Listener (on the link's thread) ----------------------------

    override fun onInfo(link: TagLink, raw: ByteArray) = updateMeta(link) {
        it.infoHex = Protocol.hexGroups(raw)
    }

    override fun onRecord(link: TagLink, kind: Int, elapsedNs: Long, payload: ByteArray) {
        var rec = current ?: return
        while (true) {
            val log = rec.logs[link.address] ?: return
            if (log.append(elapsedNs, kind, payload)) return
            // Closed: either stopping (drop it, as before) or split between
            // reading current and appending (write it to the new session).
            val next = current
            if (next == null || next === rec) return
            rec = next
        }
    }

    override fun onStatus(link: TagLink, status: Protocol.Status) {
        if (status.state != Protocol.STATE_RECORDING) return
        val rec = current ?: return
        val t = rec.meta.tags.firstOrNull { it.address == link.address } ?: return
        // session.json has room for one tag-side session; keep the first one.
        // A restart mid-recording is still visible in the logged Status records.
        if (t.sessionId != null && t.position == Protocol.positionName(status.position)) return
        updateMeta(link) {
            if (it.sessionId == null) {
                it.sessionId = status.sessionId
                it.odrHz = status.odrHz
                it.accelRangeG = status.accelRangeG
                it.gyroRangeDps = status.gyroRangeDps
            }
            it.position = Protocol.positionName(status.position)
        }
    }

    private fun updateMeta(link: TagLink, f: (TagMeta) -> Unit) {
        while (true) {
            val rec = current ?: return
            synchronized(rec.meta) {
                // Split while waiting for the lock: apply it to the new session.
                if (current === rec) {
                    val t = rec.meta.tags.firstOrNull { it.address == link.address } ?: return
                    f(t)
                    writeSessionJson(rec.dir, rec.meta)
                    return
                }
            }
        }
    }

    // ---- notification -------------------------------------------------------

    private fun summary(rec: Recording): String {
        val secs = (SystemClock.elapsedRealtimeNanos() - rec.startElapsedNs) / 1_000_000_000
        val tags = rec.links.joinToString(" · ") { l ->
            when {
                l.error != null -> "error"
                !l.connected -> "reconnecting"
                else -> l.status?.let { Protocol.stateName(it.state) } ?: "connecting"
            }
        }
        return "%d:%02d:%02d · %s".format(secs / 3600, secs / 60 % 60, secs % 60, tags)
    }

    private fun notification(text: String): Notification {
        val nm = getSystemService(NotificationManager::class.java)
        nm.createNotificationChannel(NotificationChannel(CHANNEL, "Recording",
            NotificationManager.IMPORTANCE_LOW))
        val open = PendingIntent.getActivity(this, 0, Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE)
        val stop = PendingIntent.getService(this, 1, Intent(this, RecorderService::class.java)
            .setAction(ACTION_STOP), PendingIntent.FLAG_IMMUTABLE)
        val split = PendingIntent.getService(this, 2, Intent(this, RecorderService::class.java)
            .setAction(ACTION_SPLIT), PendingIntent.FLAG_IMMUTABLE)
        return Notification.Builder(this, CHANNEL)
            .setSmallIcon(R.drawable.ic_notification)
            .setContentTitle("Recording skate session")
            .setContentText(text)
            .setOngoing(true)
            .setOnlyAlertOnce(true)
            .setContentIntent(open)
            .addAction(Notification.Action.Builder(null, "New session", split).build())
            .addAction(Notification.Action.Builder(null, "Stop", stop).build())
            .build()
    }

    private fun granted(p: String) = checkSelfPermission(p) == PackageManager.PERMISSION_GRANTED

    companion object {
        private const val TAG = "BladingTag"
        private const val CHANNEL = "recording"
        private const val NOTIFICATION_ID = 1
        private const val TICK_MS = 5_000L
        const val ACTION_START = "app.bladingtag.START"
        const val ACTION_STOP = "app.bladingtag.STOP"
        const val ACTION_SPLIT = "app.bladingtag.SPLIT"
        const val EXTRA_ADDRESSES = "addresses"
        const val EXTRA_GPS = "gps"

        @Volatile var current: Recording? = null
            private set

        fun sessionsDir(c: Context) = File(c.getExternalFilesDir(null), "sessions").apply { mkdirs() }

        /** Without [gps] there is no gps.csv (PROTOCOL.md §2 allows it to be missing). */
        fun start(c: Context, addresses: List<String>, gps: Boolean) {
            c.startForegroundService(Intent(c, RecorderService::class.java)
                .setAction(ACTION_START)
                .putExtra(EXTRA_ADDRESSES, addresses.toTypedArray())
                .putExtra(EXTRA_GPS, gps))
        }

        fun stop(c: Context) {
            c.startService(Intent(c, RecorderService::class.java).setAction(ACTION_STOP))
        }

        /** Save the current session and keep recording into a new one. */
        fun split(c: Context) {
            c.startService(Intent(c, RecorderService::class.java).setAction(ACTION_SPLIT))
        }

        /** Sets the description (session.json "notes") of the session being recorded. */
        fun setNotes(rec: Recording, notes: String) {
            synchronized(rec.meta) {
                rec.meta.notes = notes
                writeSessionJson(rec.dir, rec.meta)
            }
        }
    }
}
