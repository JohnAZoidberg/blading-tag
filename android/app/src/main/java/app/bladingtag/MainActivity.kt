package app.bladingtag

import android.Manifest
import android.annotation.SuppressLint
import android.app.Activity
import android.app.AlertDialog
import android.bluetooth.BluetoothAdapter
import android.bluetooth.BluetoothManager
import android.bluetooth.le.ScanCallback
import android.bluetooth.le.ScanFilter
import android.bluetooth.le.ScanResult
import android.bluetooth.le.ScanSettings
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Typeface
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.os.ParcelUuid
import android.os.SystemClock
import android.view.Gravity
import android.view.View
import android.view.WindowInsets
import android.widget.Button
import android.widget.CheckBox
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import android.widget.Toast
import java.util.concurrent.Executors

/**
 * One screen: find tags and start a recording, watch it while it runs, and
 * manage past sessions. The recording itself lives in [RecorderService], so
 * this activity can come and go.
 */
@SuppressLint("MissingPermission") // every BLE call is behind hasPermissions()
class MainActivity : Activity() {

    /** A tag seen while scanning. */
    private class Found(val address: String, val name: String) {
        var adv: Protocol.Adv? = null
        var rssi = 0
        var seenAt = 0L
    }

    private val ui = Handler(Looper.getMainLooper())
    private val io = Executors.newSingleThreadExecutor()
    private val found = LinkedHashMap<String, Found>()
    private val selected = LinkedHashSet<String>()
    private val rowText = HashMap<String, TextView>()
    private val sleepButtons = HashMap<String, Button>()
    private var scanning = false
    private var shownRecording: RecorderService.Recording? = null

    private lateinit var notice: TextView
    private lateinit var noticeButton: Button
    private lateinit var recordingBox: LinearLayout
    private lateinit var recordingText: TextView
    private lateinit var tagsBox: LinearLayout
    private lateinit var tagList: LinearLayout
    private lateinit var startButton: Button
    private lateinit var sessionList: LinearLayout

    private fun dp(v: Int) = (v * resources.displayMetrics.density).toInt()

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        actionBar?.hide()
        // Debug builds show over the lock screen, so the app can be driven
        // over adb (`make flash`, input taps, screenshots) without unlocking.
        if (BuildConfig.DEBUG) {
            setShowWhenLocked(true)
            setTurnScreenOn(true)
        }

        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(20), dp(20), dp(20), dp(20))
        }
        root.addView(TextView(this).apply {
            text = getString(R.string.app_name)
            textSize = 28f
            typeface = Typeface.DEFAULT_BOLD
        })

        notice = TextView(this).apply { textSize = 15f; setTextColor(COLOR_BAD) }
        noticeButton = Button(this)
        root.addView(notice, lp(dp(12)))
        root.addView(noticeButton, lp(dp(4)))

        // While recording
        recordingBox = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        recordingBox.addView(heading("Recording"))
        recordingText = TextView(this).apply { textSize = 15f; typeface = Typeface.MONOSPACE }
        recordingBox.addView(recordingText, lp(dp(8)))
        recordingBox.addView(Button(this).apply {
            text = "Stop recording"
            setOnClickListener { RecorderService.stop(this@MainActivity) }
        }, lp(dp(12)))
        root.addView(recordingBox, lp(dp(8)))

        // While idle
        tagsBox = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        tagsBox.addView(heading("Tags"))
        tagsBox.addView(TextView(this).apply {
            text = "Move the skate to wake the tag. Tick one tag, or two (one per foot)."
            textSize = 14f
            alpha = 0.7f
        }, lp(dp(4)))
        tagList = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        tagsBox.addView(tagList, lp(dp(8)))
        startButton = Button(this).apply {
            text = "Start recording"
            setOnClickListener { startRecording() }
        }
        tagsBox.addView(startButton, lp(dp(12)))
        tagsBox.addView(TextView(this).apply {
            text = "After starting, stand still for 2 seconds, then skate."
            textSize = 14f
            alpha = 0.7f
        }, lp(dp(4)))
        root.addView(tagsBox, lp(dp(8)))

        root.addView(heading("Sessions"), lp(dp(24)))
        sessionList = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        root.addView(sessionList, lp(dp(4)))

        val scroll = ScrollView(this).apply { addView(root) }
        // targetSdk 35+ draws edge to edge; keep content out of the bars.
        scroll.setOnApplyWindowInsetsListener { v, insets ->
            val bars = insets.getInsets(WindowInsets.Type.systemBars())
            v.setPadding(bars.left, bars.top, bars.right, bars.bottom)
            insets
        }
        setContentView(scroll)

        if (BuildConfig.DEBUG) {
            found[SimulatedTag.ADDRESS] = Found(SimulatedTag.ADDRESS, "Simulated tag (debug build)")
        }
    }

    override fun onResume() {
        super.onResume()
        if (!hasPermissions()) requestPermissions(PERMISSIONS, REQ_PERMISSIONS)
        refreshSessions()
        refresh()
    }

    override fun onPause() {
        stopScan()
        ui.removeCallbacks(tick)
        super.onPause()
    }

    override fun onDestroy() {
        io.shutdown()
        super.onDestroy()
    }

    override fun onRequestPermissionsResult(code: Int, perms: Array<out String>, results: IntArray) {
        refresh()
    }

    private val tick = Runnable { refresh() }

    /** Redraws the live parts; runs every 500 ms while visible. */
    private fun refresh() {
        ui.removeCallbacks(tick)
        ui.postDelayed(tick, 500)
        val rec = RecorderService.current
        if (shownRecording != null && rec == null) refreshSessions() // a recording just ended
        shownRecording = rec

        val adapter = getSystemService(BluetoothManager::class.java).adapter
        when {
            !hasPermissions() -> notice("Bluetooth and notification permissions are needed.",
                "Grant permissions") { openAppSettings() }
            adapter == null || !adapter.isEnabled -> notice("Bluetooth is off.", "Turn on Bluetooth") {
                startActivity(Intent(BluetoothAdapter.ACTION_REQUEST_ENABLE))
            }
            else -> notice(null, null) {}
        }

        recordingBox.visibility = if (rec != null) View.VISIBLE else View.GONE
        tagsBox.visibility = if (rec == null) View.VISIBLE else View.GONE
        if (rec != null) {
            stopScan()
            recordingText.text = describe(rec)
        } else {
            if (hasPermissions() && adapter?.isEnabled == true) startScan() else stopScan()
            renderTags()
        }
    }

    private fun notice(text: String?, button: String?, action: () -> Unit) {
        notice.visibility = if (text != null) View.VISIBLE else View.GONE
        notice.text = text
        noticeButton.visibility = if (button != null) View.VISIBLE else View.GONE
        noticeButton.text = button
        noticeButton.setOnClickListener { action() }
    }

    private fun describe(rec: RecorderService.Recording): String {
        val sb = StringBuilder()
        val secs = (SystemClock.elapsedRealtimeNanos() - rec.startElapsedNs) / 1_000_000_000
        sb.append("%d:%02d:%02d".format(secs / 3600, secs / 60 % 60, secs % 60))
        if (rec.stopping) sb.append("  stopping…")
        sb.append("\n")
        for (l in rec.links) {
            val s = l.status
            sb.append("\n").append(if (l.address == SimulatedTag.ADDRESS) "simulated" else l.address)
            s?.let { sb.append("  ").append(Protocol.positionName(it.position)) }
            sb.append("\n  ")
            sb.append(when {
                !l.connected && l.packets == 0L -> "connecting…"
                !l.connected -> "link lost, reconnecting… (tag keeps recording)"
                s == null -> "setting up…"
                else -> Protocol.stateName(s.state)
            })
            l.error?.let { sb.append("\n  ! ").append(it) }
            sb.append("\n  samples ").append(l.samples)
            val rate = l.measuredRate
            if (!rate.isNaN()) sb.append("  @ %.1f Hz".format(rate))
            if (!l.accelG.isNaN()) sb.append("\n  |a| %.2f g".format(l.accelG))
            if (s != null) {
                sb.append("\n  battery ").append(s.batteryMv).append(" mV")
                if (s.flags and Protocol.FLAG_LOW_BATTERY != 0) sb.append(" LOW")
                if (s.bufferFill > 0) sb.append("  buffered ").append(s.bufferFill)
                if (s.flags and Protocol.FLAG_IMU_FAULT != 0) sb.append("\n  ! IMU fault")
            }
            val lost = maxOf(l.missingSamples, s?.samplesDropped ?: 0)
            if (lost > 0 || l.seqGaps > 0) sb.append("\n  lost ").append(lost).append(" samples, ")
                .append(l.seqGaps).append(" packets")
            if (l.reconnects > 0) sb.append("\n  reconnects ").append(l.reconnects)
            if (l.restarts > 0) sb.append(", restarted ").append(l.restarts).append("×")
            rec.logs[l.address]?.let { sb.append("\n  file ").append(size(it.bytes)) }
        }
        sb.append("\n\nGPS  ")
        val fix = rec.lastFix
        when {
            rec.gps == null -> sb.append("off (no location permission)")
            fix == null -> sb.append("waiting for fix…")
            else -> {
                val age = (SystemClock.elapsedRealtimeNanos() - fix.elapsedRealtimeNanos) / 1_000_000_000
                sb.append("±%.0f m".format(fix.accuracy))
                if (fix.hasSpeed()) sb.append("  %.1f km/h".format(fix.speed * 3.6))
                if (age > 3) sb.append("  (${age}s old)")
                sb.append("\n     ").append(rec.gps.fixes).append(" fixes")
            }
        }
        return sb.toString()
    }

    // ---- tags list ----------------------------------------------------------

    private fun renderTags() {
        val now = SystemClock.elapsedRealtime()
        // Forget tags not heard from in a while (asleep, or out of range).
        found.values.removeAll { it.address != SimulatedTag.ADDRESS && now - it.seenAt > 30_000 }
        selected.retainAll(found.keys)
        if (rowText.keys != found.keys) {
            tagList.removeAllViews()
            rowText.clear()
            sleepButtons.clear()
            if (found.isEmpty()) tagList.addView(TextView(this).apply {
                text = if (scanning) "Searching…" else "Not searching."
                textSize = 15f
            })
            for (f in found.values) tagList.addView(tagRow(f))
        }
        for (f in found.values) {
            rowText[f.address]?.text = rowDescription(f)
            // The tag refuses SLEEP while recording (BAD_STATE); stop it first.
            sleepButtons[f.address]?.isEnabled = f.adv?.state != Protocol.STATE_RECORDING
        }
        startButton.isEnabled = selected.isNotEmpty() && hasPermissions()
    }

    private fun rowDescription(f: Found): String {
        if (f.address == SimulatedTag.ADDRESS) return "Fake 200 Hz data, no hardware needed"
        val a = f.adv ?: return "${f.address}  ${f.rssi} dBm"
        val parts = mutableListOf(Protocol.stateName(a.state), Protocol.positionName(a.position),
            "${a.batteryMv} mV", "${f.rssi} dBm")
        if (a.version != Protocol.VERSION) parts.add(0, "protocol v${a.version}, update needed")
        return parts.joinToString(" · ")
    }

    private fun tagRow(f: Found): View {
        val row = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(0, dp(6), 0, dp(6))
        }
        row.addView(CheckBox(this).apply {
            text = f.name
            textSize = 17f
            isChecked = f.address in selected
            setOnCheckedChangeListener { b, on ->
                if (on && selected.size >= 2) {
                    b.isChecked = false
                    toast("At most two tags")
                } else if (on) selected.add(f.address) else selected.remove(f.address)
                startButton.isEnabled = selected.isNotEmpty()
            }
        })
        val desc = TextView(this).apply {
            textSize = 14f
            alpha = 0.75f
            setPadding(dp(32), 0, 0, 0)
        }
        rowText[f.address] = desc
        row.addView(desc)
        if (f.address != SimulatedTag.ADDRESS) {
            val buttons = LinearLayout(this).apply {
                orientation = LinearLayout.HORIZONTAL
                setPadding(dp(28), 0, 0, 0)
            }
            fun b(into: LinearLayout, label: String, cmd: ByteArray, done: String, onOk: () -> Unit = {}) =
                Button(this).apply {
                    text = label
                    isAllCaps = false
                    setOnClickListener { oneShot(f.address, cmd, done, onOk) }
                    into.addView(this)
                }
            b(buttons, "Blink", Protocol.identify(5), "Blinking")
            b(buttons, "Left foot", Protocol.setPosition(1), "Set to left")
            b(buttons, "Right foot", Protocol.setPosition(2), "Set to right")
            row.addView(buttons)
            // Own line: a 4th button doesn't fit, and it keeps the one that
            // takes the tag away apart from the harmless ones.
            val power = LinearLayout(this).apply {
                orientation = LinearLayout.HORIZONTAL
                setPadding(dp(28), 0, 0, 0)
            }
            // System OFF: the tag stops advertising, so drop it from the list
            // now rather than after the 30 s timeout. Only its button (or,
            // once it works, motion) wakes it again.
            sleepButtons[f.address] = b(power, "Sleep", Protocol.sleep(),
                "Asleep. Press its button to wake it") { found.remove(f.address) }
            row.addView(power)
        }
        return row
    }

    /** Connect just long enough to send one Control command. */
    private fun oneShot(address: String, cmd: ByteArray, okText: String, onOk: () -> Unit = {}) {
        if (!hasPermissions()) return
        toast("Connecting…")
        stopScan() // scanning slows connection setup on many phones
        val quiet = object : TagLink.Listener {
            override fun onInfo(link: TagLink, raw: ByteArray) {}
            override fun onRecord(link: TagLink, kind: Int, elapsedNs: Long, payload: ByteArray) {}
        }
        GattTag(this, address, quiet, oneShot = cmd, onOneShotDone = { err ->
            ui.post {
                toast(err?.let { "Failed: $it" } ?: okText)
                if (err == null) onOk()
                // The tag readvertises with the new state/position.
                if (!isFinishing) refresh()
            }
        }).start()
    }

    private fun startRecording() {
        if (selected.isEmpty()) return
        stopScan()
        RecorderService.start(this, selected.toList())
        selected.clear()
        ui.postDelayed({ refresh() }, 200)
    }

    // ---- scanning -----------------------------------------------------------

    private val scanCallback = object : ScanCallback() {
        override fun onScanResult(callbackType: Int, r: ScanResult) {
            val addr = r.device.address
            val f = found.getOrPut(addr) {
                Found(addr, r.scanRecord?.deviceName ?: r.device.name ?: "BladeTag-" +
                    addr.takeLast(5).replace(":", ""))
            }
            f.rssi = r.rssi
            f.seenAt = SystemClock.elapsedRealtime()
            r.scanRecord?.getManufacturerSpecificData(Protocol.COMPANY_ID)?.let { f.adv = Protocol.parseAdv(it) }
        }

        override fun onScanFailed(errorCode: Int) {
            scanning = false
            notice("Bluetooth scan failed ($errorCode).", null) {}
        }
    }

    private fun startScan() {
        if (scanning) return
        val scanner = getSystemService(BluetoothManager::class.java).adapter?.bluetoothLeScanner ?: return
        val filter = ScanFilter.Builder().setServiceUuid(ParcelUuid(Protocol.SERVICE)).build()
        val settings = ScanSettings.Builder().setScanMode(ScanSettings.SCAN_MODE_LOW_LATENCY).build()
        scanner.startScan(listOf(filter), settings, scanCallback)
        scanning = true
    }

    private fun stopScan() {
        if (!scanning) return
        scanning = false
        if (!hasPermissions()) return
        getSystemService(BluetoothManager::class.java).adapter?.bluetoothLeScanner?.stopScan(scanCallback)
    }

    // ---- sessions -----------------------------------------------------------

    private fun refreshSessions() {
        io.execute {
            val list = Sessions.list(this)
            val active = RecorderService.current?.dir
            ui.post { renderSessions(list.filter { it.dir != active }) }
        }
    }

    private fun renderSessions(list: List<Sessions.Summary>) {
        sessionList.removeAllViews()
        if (list.isEmpty()) {
            sessionList.addView(TextView(this).apply { text = "None yet."; textSize = 15f })
            return
        }
        for (s in list) {
            val row = LinearLayout(this).apply {
                orientation = LinearLayout.HORIZONTAL
                gravity = Gravity.CENTER_VERTICAL
                setPadding(0, dp(4), 0, dp(4))
            }
            val d = s.durationS
            val dur = if (d == null) "unfinished" else "%d:%02d:%02d".format(d / 3600, d / 60 % 60, d % 60)
            row.addView(TextView(this).apply {
                text = "${s.dir.name.removePrefix("session-")}\n$dur · ${s.tags} tag · " +
                    "${s.gpsFixes} GPS · ${size(s.bytes)}"
                textSize = 14f
            }, LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1f))
            row.addView(Button(this).apply {
                text = "Share"; isAllCaps = false
                setOnClickListener { export(s) }
            })
            row.addView(Button(this).apply {
                text = "Delete"; isAllCaps = false
                setOnClickListener { confirmDelete(s) }
            })
            sessionList.addView(row)
        }
    }

    private fun export(s: Sessions.Summary) {
        toast("Saving to Downloads/blading-tag…")
        io.execute {
            val result = runCatching { Sessions.exportZip(this, s.dir) }
            ui.post {
                result.onSuccess { uri ->
                    startActivity(Intent.createChooser(Intent(Intent.ACTION_SEND)
                        .setType("application/zip")
                        .putExtra(Intent.EXTRA_STREAM, uri)
                        .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION), s.dir.name))
                }.onFailure { toast("Export failed: ${it.message}") }
            }
        }
    }

    private fun confirmDelete(s: Sessions.Summary) {
        AlertDialog.Builder(this)
            .setTitle("Delete ${s.dir.name}?")
            .setMessage("This removes the recording from the phone. Exported copies in Downloads stay.")
            .setPositiveButton("Delete") { _, _ ->
                io.execute { s.dir.deleteRecursively(); refreshSessions() }
            }
            .setNegativeButton("Cancel", null)
            .show()
    }

    // ---- helpers ------------------------------------------------------------

    private fun heading(t: String) = TextView(this).apply {
        text = t
        textSize = 20f
        typeface = Typeface.DEFAULT_BOLD
    }

    private fun lp(topMargin: Int) = LinearLayout.LayoutParams(
        LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT
    ).apply { this.topMargin = topMargin }

    private fun size(bytes: Long) =
        if (bytes < 1_000_000) "%d kB".format(bytes / 1000) else "%.1f MB".format(bytes / 1e6)

    private fun toast(t: String) = Toast.makeText(this, t, Toast.LENGTH_SHORT).show()

    /** Only the permissions the app can't work without; location is optional (no gps.csv without it). */
    private fun hasPermissions() = REQUIRED.all { checkSelfPermission(it) == PackageManager.PERMISSION_GRANTED }

    private fun openAppSettings() {
        if (REQUIRED.any { shouldShowRequestPermissionRationale(it) } ||
            REQUIRED.none { checkSelfPermission(it) == PackageManager.PERMISSION_GRANTED }) {
            requestPermissions(PERMISSIONS, REQ_PERMISSIONS)
        } else {
            startActivity(Intent(android.provider.Settings.ACTION_APPLICATION_DETAILS_SETTINGS,
                android.net.Uri.fromParts("package", packageName, null)))
        }
    }

    companion object {
        private const val REQ_PERMISSIONS = 1
        private const val COLOR_BAD = 0xFFC62828.toInt()
        private val REQUIRED = arrayOf(
            Manifest.permission.BLUETOOTH_SCAN,
            Manifest.permission.BLUETOOTH_CONNECT,
            Manifest.permission.POST_NOTIFICATIONS,
        )
        /** Asked for together with REQUIRED; recording works without it, just no GPS track. */
        private val PERMISSIONS = REQUIRED + Manifest.permission.ACCESS_FINE_LOCATION
    }
}
