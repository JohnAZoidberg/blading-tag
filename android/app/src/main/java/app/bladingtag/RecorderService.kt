package app.bladingtag

import android.Manifest
import android.annotation.SuppressLint
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.bluetooth.BluetoothManager
import android.bluetooth.le.ScanCallback
import android.bluetooth.le.ScanFilter
import android.bluetooth.le.ScanResult
import android.bluetooth.le.ScanSettings
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
import android.os.ParcelUuid
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
 *
 * **Standby** ([standby]): armed from the app, the service waits in the
 * foreground with a filtered BLE scan for one of its tags advertising
 * RECORDING, i.e. started with its button. Then it records from all of its
 * tags (starting the others) as if Start had been tapped; the tag's buffer
 * covers the few seconds until the phone notices. A tag's button stopping
 * it ends the recording, and the service goes back to waiting until standby
 * is left. Because the service is already in the foreground when the
 * recording starts, Android allows it BLE and GPS with the app in the
 * background and the screen off.
 */
@SuppressLint("MissingPermission") // the activity only arms standby/records with BLE permissions
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

    /** Armed standby: which tags to watch and record, and whether to record GPS. */
    class Standby(val addresses: List<String>, val gps: Boolean)

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
            ACTION_STANDBY -> if (current == null && standby == null) {
                arm(intent.getStringArrayExtra(EXTRA_ADDRESSES)?.toList().orEmpty(),
                    intent.getBooleanExtra(EXTRA_GPS, true))
            }
            ACTION_LEAVE_STANDBY -> disarm()
        }
        return START_NOT_STICKY
    }

    /** [wantGps]: the user's choice; GPS also needs the location permission. */
    private fun begin(addresses: List<String>, wantGps: Boolean) {
        val hasLocation = wantGps && granted(Manifest.permission.ACCESS_FINE_LOCATION)
        if (standby == null) {
            startForeground(NOTIFICATION_ID, notification("Starting…"), fgsTypes(hasLocation))
        } else {
            // Already in the foreground with these types since arm(). Calling
            // startForeground again now, from the background, could lose the
            // location type.
            getSystemService(NotificationManager::class.java)
                .notify(NOTIFICATION_ID, notification("Starting…"))
        }

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

    private fun fgsTypes(location: Boolean): Int {
        var types = ServiceInfo.FOREGROUND_SERVICE_TYPE_CONNECTED_DEVICE
        if (location) types = types or ServiceInfo.FOREGROUND_SERVICE_TYPE_LOCATION
        return types
    }

    // ---- standby ------------------------------------------------------------

    /** [wantGps]: as for [begin]. Must be called while the app is visible. */
    private fun arm(addresses: List<String>, wantGps: Boolean) {
        val hasLocation = wantGps && granted(Manifest.permission.ACCESS_FINE_LOCATION)
        val tags = addresses.filter { it != SimulatedTag.ADDRESS }
        if (tags.isEmpty()) { stopSelf(); return }
        startForeground(NOTIFICATION_ID, standbyNotification(), fgsTypes(hasLocation))
        standby = Standby(tags, hasLocation)
        Log.i(TAG, "standby for $tags, gps=$hasLocation")
        startStandbyScan()
    }

    /** Leave standby. A recording in progress carries on; it stops the service when it ends. */
    private fun disarm() {
        if (standby == null) return
        standby = null
        stopStandbyScan()
        Log.i(TAG, "standby left")
        if (current == null) {
            stopForeground(STOP_FOREGROUND_REMOVE)
            stopSelf()
        } else {
            current?.let { rec ->
                getSystemService(NotificationManager::class.java)
                    .notify(NOTIFICATION_ID, notification(summary(rec)))
            }
        }
    }

    private var scanning = false

    private val scanCallback = object : ScanCallback() {
        override fun onScanResult(callbackType: Int, r: ScanResult) = onAdvert(r)
        override fun onBatchScanResults(results: MutableList<ScanResult>) = results.forEach(::onAdvert)
        override fun onScanFailed(errorCode: Int) {
            Log.w(TAG, "standby scan failed: $errorCode")
            scanning = false
            if (standby != null && current == null) {
                getSystemService(NotificationManager::class.java)
                    .notify(NOTIFICATION_ID, standbyNotification("Bluetooth scan failed ($errorCode), retrying…"))
                main.postDelayed(::startStandbyScan, SCAN_RETRY_MS)
            }
        }
    }

    /**
     * Only tags advertising RECORDING (manufacturer data byte 1, PROTOCOL.md
     * §1.1) match, so with the screen off the Bluetooth controller does the
     * filtering and the phone sleeps until a button is pressed.
     */
    private fun startStandbyScan() {
        main.removeCallbacks(restartScan)
        if (scanning || standby == null || current != null) return
        val scanner = getSystemService(BluetoothManager::class.java).adapter?.bluetoothLeScanner
        if (scanner == null) {
            // Bluetooth is off; try again later rather than give up standby.
            main.postDelayed(::startStandbyScan, SCAN_RETRY_MS)
            return
        }
        val filter = ScanFilter.Builder()
            .setServiceUuid(ParcelUuid(Protocol.SERVICE))
            .setManufacturerData(Protocol.COMPANY_ID,
                byteArrayOf(0, Protocol.STATE_RECORDING.toByte()), byteArrayOf(0, 0xFF.toByte()))
            .build()
        val settings = ScanSettings.Builder().setScanMode(ScanSettings.SCAN_MODE_LOW_POWER).build()
        scanner.startScan(listOf(filter), settings, scanCallback)
        scanning = true
        // Android quietly demotes scans that run for 30 minutes; start afresh before that.
        main.postDelayed(restartScan, SCAN_RESTART_MS)
        getSystemService(NotificationManager::class.java)
            .notify(NOTIFICATION_ID, standbyNotification())
    }

    private val restartScan = Runnable {
        stopStandbyScan()
        startStandbyScan()
    }

    private fun stopStandbyScan() {
        main.removeCallbacks(restartScan)
        if (!scanning) return
        scanning = false
        runCatching {
            getSystemService(BluetoothManager::class.java).adapter?.bluetoothLeScanner?.stopScan(scanCallback)
        }
    }

    /** On the main thread (BluetoothLeScanner delivers results there). */
    private fun onAdvert(r: ScanResult) {
        val sb = standby ?: return
        if (current != null || r.device.address !in sb.addresses) return
        val adv = r.scanRecord?.getManufacturerSpecificData(Protocol.COMPANY_ID)
            ?.let { Protocol.parseAdv(it) } ?: return
        if (adv.state != Protocol.STATE_RECORDING || adv.version != Protocol.VERSION) return
        Log.i(TAG, "standby: ${r.device.address} is recording, joining")
        stopStandbyScan()
        begin(sb.addresses, sb.gps)
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
        if (rec == null) {
            // Stop with nothing recording ends standby too.
            if (standby != null) disarm() else stopSelf()
            return
        }
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
            if (standby != null) {
                startStandbyScan()
            } else {
                stopForeground(STOP_FOREGROUND_REMOVE)
                stopSelf()
            }
        }
        if (pending == 0) { finish(); return }
        rec.links.forEach { link ->
            link.stop { main.post { if (--pending == 0) finish() } }
        }
    }

    override fun onDestroy() {
        standby = null
        stopStandbyScan()
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

    /** A tag's button stopped it: end the whole recording, stopping the other tag too. */
    override fun onTagStopped(link: TagLink) {
        main.post {
            // Only the recording this link belongs to (not a later one).
            if (current?.links?.contains(link) != true) return@post
            Log.i(TAG, "${link.address} was stopped on the tag, ending the recording")
            end()
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

    private fun standbyNotification(text: String? = null): Notification {
        val nm = getSystemService(NotificationManager::class.java)
        nm.createNotificationChannel(NotificationChannel(CHANNEL, "Recording",
            NotificationManager.IMPORTANCE_LOW))
        val open = PendingIntent.getActivity(this, 0, Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE)
        val leave = PendingIntent.getService(this, 3, Intent(this, RecorderService::class.java)
            .setAction(ACTION_LEAVE_STANDBY), PendingIntent.FLAG_IMMUTABLE)
        return Notification.Builder(this, CHANNEL)
            .setSmallIcon(R.drawable.ic_notification)
            .setContentTitle("Standby")
            .setContentText(text ?: "Press the button on a tag to start recording")
            .setOngoing(true)
            .setOnlyAlertOnce(true)
            .setContentIntent(open)
            .addAction(Notification.Action.Builder(null, "Leave standby", leave).build())
            .build()
    }

    private fun granted(p: String) = checkSelfPermission(p) == PackageManager.PERMISSION_GRANTED

    companion object {
        private const val TAG = "BladingTag"
        private const val CHANNEL = "recording"
        private const val NOTIFICATION_ID = 1
        private const val TICK_MS = 5_000L
        private const val SCAN_RESTART_MS = 20 * 60_000L
        private const val SCAN_RETRY_MS = 10_000L
        const val ACTION_START = "app.bladingtag.START"
        const val ACTION_STOP = "app.bladingtag.STOP"
        const val ACTION_SPLIT = "app.bladingtag.SPLIT"
        const val ACTION_STANDBY = "app.bladingtag.STANDBY"
        const val ACTION_LEAVE_STANDBY = "app.bladingtag.LEAVE_STANDBY"
        const val EXTRA_ADDRESSES = "addresses"
        const val EXTRA_GPS = "gps"

        @Volatile var current: Recording? = null
            private set

        /** Non-null while standby is armed (also during a recording it started). */
        @Volatile var standby: Standby? = null
            private set

        fun sessionsDir(c: Context) = File(c.getExternalFilesDir(null), "sessions").apply { mkdirs() }

        /** Without [gps] there is no gps.csv (PROTOCOL.md §2 allows it to be missing). */
        fun start(c: Context, addresses: List<String>, gps: Boolean) {
            c.startForegroundService(Intent(c, RecorderService::class.java)
                .setAction(ACTION_START)
                .putExtra(EXTRA_ADDRESSES, addresses.toTypedArray())
                .putExtra(EXTRA_GPS, gps))
        }

        /** Wait for one of [addresses] to be started with its button, then record from all of them.
         *  Call while the app is visible: the foreground service has to start from there. */
        fun standby(c: Context, addresses: List<String>, gps: Boolean) {
            c.startForegroundService(Intent(c, RecorderService::class.java)
                .setAction(ACTION_STANDBY)
                .putExtra(EXTRA_ADDRESSES, addresses.toTypedArray())
                .putExtra(EXTRA_GPS, gps))
        }

        fun leaveStandby(c: Context) {
            c.startService(Intent(c, RecorderService::class.java).setAction(ACTION_LEAVE_STANDBY))
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
