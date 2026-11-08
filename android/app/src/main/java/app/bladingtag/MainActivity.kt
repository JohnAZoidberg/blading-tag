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
import android.text.InputType
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.os.ParcelUuid
import android.os.SystemClock
import android.view.Gravity
import android.view.View
import android.view.WindowInsets
import android.widget.Button
import android.widget.ArrayAdapter
import android.widget.CheckBox
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.Spinner
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
    /** Set once the user ticks or unticks a tag; stops [autoSelect]. */
    private var userPicked = false
    private val rowText = HashMap<String, TextView>()
    /** Per tag: the Stop and Sleep buttons, enabled by advertised state. */
    private val powerButtons = HashMap<String, Pair<Button, Button>>()
    private var scanning = false
    private var shownRecording: RecorderService.Recording? = null

    private lateinit var notice: TextView
    private lateinit var noticeButton: Button
    private lateinit var recordingBox: LinearLayout
    private lateinit var recordingText: TextView
    private lateinit var livePanel: LivePanel
    private lateinit var tagsBox: LinearLayout
    private lateinit var tagList: LinearLayout
    private lateinit var startButton: Button
    private lateinit var standbyButton: Button
    private lateinit var standbyBox: LinearLayout
    private lateinit var standbyText: TextView
    private lateinit var gpsBox: CheckBox
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
        livePanel = LivePanel(this)
        recordingBox.addView(livePanel, lp(dp(8)))
        recordingText = TextView(this).apply { textSize = 15f; typeface = Typeface.MONOSPACE }
        recordingBox.addView(recordingText, lp(dp(16)))
        recordingBox.addView(Button(this).apply {
            text = "Description…"
            setOnClickListener { editCurrentNotes() }
        }, lp(dp(12)))
        recordingBox.addView(Button(this).apply {
            text = "Save and start new session"
            setOnClickListener {
                RecorderService.split(this@MainActivity)
                toast("Saved. Recording a new session")
            }
        }, lp(dp(4)))
        recordingBox.addView(TextView(this).apply {
            text = "Keeps recording without a gap, so each movement pattern gets its own session."
            textSize = 14f
            alpha = 0.7f
        }, lp(dp(2)))
        recordingBox.addView(Button(this).apply {
            text = "Stop recording"
            setOnClickListener { RecorderService.stop(this@MainActivity) }
        }, lp(dp(12)))
        root.addView(recordingBox, lp(dp(8)))

        // Standby, waiting for a tag's button
        standbyBox = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        standbyBox.addView(heading("Standby"))
        standbyText = TextView(this).apply { textSize = 15f }
        standbyBox.addView(standbyText, lp(dp(8)))
        standbyBox.addView(TextView(this).apply {
            text = "On the tag:\n" +
                "• green flash: the tag started recording\n" +
                "• blue blinking a few seconds later: the phone joined and records it\n" +
                "• yellow blip every second: no phone yet, the tag buffers (~70 s)\n" +
                "• red blip every second: buffer full, data is being lost\n" +
                "Press the button again to stop. The phone then waits for the next press."
            textSize = 14f
            alpha = 0.7f
        }, lp(dp(8)))
        standbyBox.addView(Button(this).apply {
            text = "Leave standby"
            setOnClickListener {
                RecorderService.leaveStandby(this@MainActivity)
                ui.postDelayed({ refresh() }, 200)
            }
        }, lp(dp(12)))
        root.addView(standbyBox, lp(dp(8)))

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
        gpsBox = CheckBox(this).apply {
            text = "Record GPS (speed, for distance per push)"
            textSize = 15f
            isChecked = prefs().getBoolean(PREF_GPS, true)
            setOnCheckedChangeListener { _, on ->
                prefs().edit().putBoolean(PREF_GPS, on).apply()
                if (on && !hasLocation()) requestPermissions(arrayOf(LOCATION), REQ_PERMISSIONS)
            }
        }
        tagsBox.addView(gpsBox, lp(dp(8)))
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
        standbyButton = Button(this).apply {
            text = "Standby: start with the tag's button"
            setOnClickListener { enterStandby() }
        }
        tagsBox.addView(standbyButton, lp(dp(12)))
        tagsBox.addView(TextView(this).apply {
            text = "Put the phone away. Pressing a ticked tag's button starts recording " +
                "from all ticked tags, pressing it again stops."
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
        if (!hasPermissions()) requestPermissions(wantedPermissions(), REQ_PERMISSIONS)
        refreshSessions()
        refresh()
    }

    override fun onPause() {
        stopScan()
        ui.removeCallbacks(tick)
        ui.removeCallbacks(frame)
        super.onPause()
    }

    override fun onDestroy() {
        io.shutdown()
        super.onDestroy()
    }

    override fun onRequestPermissionsResult(code: Int, perms: Array<out String>, results: IntArray) {
        // GPS was asked for but location was refused: show it as off rather
        // than silently recording without it.
        if (LOCATION in perms && !hasLocation() && gpsBox.isChecked) {
            gpsBox.isChecked = false
            toast("GPS is off: it needs the location permission")
        }
        refresh()
    }

    private val tick = Runnable { refresh() }

    /** Redraws the motion plots at ~20 fps while a recording is shown. */
    private val frame = object : Runnable {
        override fun run() {
            val rec = RecorderService.current ?: return
            livePanel.animate(rec)
            ui.postDelayed(this, FRAME_MS)
        }
    }

    /** Redraws the live parts; runs every 500 ms while visible. */
    private fun refresh() {
        ui.removeCallbacks(tick)
        ui.postDelayed(tick, 500)
        val rec = RecorderService.current
        // A recording just ended, or was split: a session was saved.
        if (shownRecording != null && rec !== shownRecording) refreshSessions()
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

        val standby = RecorderService.standby
        recordingBox.visibility = if (rec != null) View.VISIBLE else View.GONE
        standbyBox.visibility = if (rec == null && standby != null) View.VISIBLE else View.GONE
        tagsBox.visibility = if (rec == null && standby == null) View.VISIBLE else View.GONE
        if (rec != null) {
            stopScan()
            recordingText.text = describe(rec)
            livePanel.update(rec)
            if (!ui.hasCallbacks(frame)) ui.post(frame)
        } else if (standby != null) {
            // The service scans for the tags; a second scan here would only compete.
            stopScan()
            standbyText.text = "Waiting for the button on " +
                standby.addresses.joinToString(" or ") { it.takeLast(5) } +
                (if (standby.gps) ". GPS on." else ". GPS off.")
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
        // Elapsed time and "stopping…" are at the top of the live panel.
        if (RecorderService.standby != null) sb.append("standby: back to waiting for the button after this\n")
        rec.meta.notes.takeIf { it.isNotBlank() }?.let { sb.append(it.lines().first()).append("\n") }
        for (l in rec.links) {
            val s = l.status
            sb.append("\n").append(if (l.address == SimulatedTag.ADDRESS) "simulated" else l.address)
            s?.let { sb.append("  ").append(Protocol.positionLabel(Protocol.positionName(it.position))) }
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
            rec.gps == null -> sb.append("off")
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
            powerButtons.clear()
            if (found.isEmpty()) tagList.addView(TextView(this).apply {
                text = if (scanning) "Searching…" else "Not searching."
                textSize = 15f
            })
            for (f in found.values) tagList.addView(tagRow(f))
        }
        for (f in found.values) {
            rowText[f.address]?.text = rowDescription(f)
            // This app's own recordings hide the list, so a RECORDING tag here
            // was started elsewhere (another phone/laptop, or its button).
            // The tag refuses SLEEP while recording (BAD_STATE): Stop first.
            val recording = f.adv?.state == Protocol.STATE_RECORDING
            powerButtons[f.address]?.let { (stop, sleep) ->
                stop.isEnabled = recording
                sleep.isEnabled = !recording
            }
        }
        startButton.isEnabled = selected.isNotEmpty() && hasPermissions()
        standbyButton.isEnabled = selected.any { it != SimulatedTag.ADDRESS } && hasPermissions()
    }

    private fun rowDescription(f: Found): String {
        if (f.address == SimulatedTag.ADDRESS) return "Fake 200 Hz data, no hardware needed"
        val a = f.adv ?: return "${f.address}  ${f.rssi} dBm"
        val parts = mutableListOf(Protocol.stateName(a.state),
            Protocol.positionLabel(Protocol.positionName(a.position)),
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
                userPicked = true
                startButton.isEnabled = selected.isNotEmpty()
                standbyButton.isEnabled = selected.any { it != SimulatedTag.ADDRESS }
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
            buttons.addView(Button(this).apply {
                text = "Body location…"
                isAllCaps = false
                setOnClickListener { pickBodyLocation(f) }
            })
            row.addView(buttons)
            // Own line: more buttons don't fit, and it keeps the ones that
            // end something apart from the harmless ones.
            val power = LinearLayout(this).apply {
                orientation = LinearLayout.HORIZONTAL
                setPadding(dp(28), 0, 0, 0)
            }
            // Without Data notifications the tag drops what it still has
            // buffered and goes READY at once (whoever started it loses
            // that tail).
            val stop = b(power, "Stop", Protocol.stop(), "Stopped")
            // System OFF: the tag stops advertising, so drop it from the list
            // now rather than after the 30 s timeout. Only its button (or,
            // once it works, motion) wakes it again.
            val sleep = b(power, "Sleep", Protocol.sleep(),
                "Asleep. Press its button to wake it") { found.remove(f.address) }
            powerButtons[f.address] = stop to sleep
            row.addView(power)
        }
        return row
    }

    /** Stores on the tag where it is worn. */
    private fun pickBodyLocation(f: Found) {
        val locations = Protocol.BODY_LOCATIONS
        AlertDialog.Builder(this)
            .setTitle("Where is the tag worn?")
            .setSingleChoiceItems(locations.map { it.label }.toTypedArray(), f.adv?.position ?: -1) { d, i ->
                d.dismiss()
                oneShot(f.address, Protocol.setPosition(locations[i].value), "Set to ${locations[i].label.lowercase()}")
            }
            .setNegativeButton("Cancel", null)
            .show()
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
        RecorderService.start(this, selected.toList(), gps = gpsBox.isChecked && hasLocation())
        rememberTags()
        selected.clear()
        userPicked = false
        ui.postDelayed({ refresh() }, 200)
    }

    private fun enterStandby() {
        val tags = selected.filter { it != SimulatedTag.ADDRESS }
        if (tags.isEmpty()) return
        stopScan()
        // From here, while the app is visible: Android only lets a foreground
        // service use GPS in the background if it was started in the foreground.
        RecorderService.standby(this, tags, gps = gpsBox.isChecked && hasLocation())
        rememberTags()
        selected.clear()
        userPicked = false
        ui.postDelayed({ refresh() }, 200)
    }

    /** Prefer the same tags next time, see [autoSelect]. */
    private fun rememberTags() {
        prefs().edit().putStringSet(PREF_TAGS, selected.filter { it != SimulatedTag.ADDRESS }.toSet()).apply()
    }

    /**
     * Tick tags as they show up, so the usual case is just pressing Start.
     * At most two; a tag used last time takes the place of one that wasn't.
     */
    private fun autoSelect(addr: String) {
        if (userPicked) return
        val remembered = prefs().getStringSet(PREF_TAGS, emptySet())!!
        if (selected.size < 2) selected.add(addr)
        else if (addr in remembered) selected.firstOrNull { it !in remembered }?.let {
            selected.remove(it)
            selected.add(addr)
        }
    }

    // ---- scanning -----------------------------------------------------------

    private val scanCallback = object : ScanCallback() {
        override fun onScanResult(callbackType: Int, r: ScanResult) {
            val addr = r.device.address
            val f = found.getOrPut(addr) {
                autoSelect(addr)
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
            val d = s.durationS
            val dur = if (d == null) "unfinished" else "%d:%02d:%02d".format(d / 3600, d / 60 % 60, d % 60)
            val feet = s.json?.tags?.joinToString(" + ") {
                if (it.simulated) "simulated" else Protocol.positionLabel(it.position).lowercase()
            }?.let { " · $it" }.orEmpty()
            val fake = if (s.json?.simulated == true) " · SIMULATED" else ""
            sessionList.addView(TextView(this).apply {
                text = "${s.dir.name.removePrefix("session-")}$fake\n$dur · ${s.tags} tag$feet · " +
                    "${s.gpsFixes} GPS · ${size(s.bytes)}"
                textSize = 14f
                setPadding(0, dp(10), 0, 0)
            })
            s.json?.notes?.takeIf { it.isNotBlank() }?.let { notes ->
                sessionList.addView(TextView(this).apply {
                    text = notes
                    textSize = 14f
                    setTypeface(typeface, Typeface.ITALIC)
                })
            }
            val buttons = LinearLayout(this).apply {
                orientation = LinearLayout.HORIZONTAL
                gravity = Gravity.CENTER_VERTICAL
            }
            fun b(label: String, action: () -> Unit) = buttons.addView(Button(this).apply {
                text = label; isAllCaps = false
                setOnClickListener { action() }
            })
            if (s.json != null) b("Edit") { edit(s, s.json) }
            b("Share") { export(s) }
            b("Delete") { confirmDelete(s) }
            sessionList.addView(buttons)
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

    private fun notesField(current: String) = EditText(this).apply {
        hint = "e.g. crossovers, double push, uphill"
        setText(current)
        setSelection(text.length)
        inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_FLAG_CAP_SENTENCES or
            InputType.TYPE_TEXT_FLAG_MULTI_LINE
    }

    /** The description of the session being recorded. */
    private fun editCurrentNotes() {
        val rec = RecorderService.current ?: return
        val field = notesField(rec.meta.notes)
        val box = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(20), dp(8), dp(20), 0)
            addView(field)
        }
        AlertDialog.Builder(this)
            .setTitle("Description")
            .setView(box)
            .setPositiveButton("Save") { _, _ ->
                val notes = field.text.toString().trim()
                io.execute { RecorderService.setNotes(rec, notes) }
            }
            .setNegativeButton("Cancel", null)
            .show()
    }

    /** Description and body location of each tag for a saved session. */
    private fun edit(s: Sessions.Summary, json: SessionJson) {
        val box = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(20), dp(8), dp(20), 0)
        }
        box.addView(TextView(this).apply { text = "Description"; textSize = 15f })
        val field = notesField(json.notes)
        box.addView(field)
        val locations = Protocol.BODY_LOCATIONS
        val choices = json.tags.map { t ->
            box.addView(TextView(this).apply {
                text = if (t.address == SimulatedTag.ADDRESS) "Simulated tag" else "Tag ${t.address}"
                textSize = 15f
            }, lp(dp(16)))
            val spinner = Spinner(this).apply {
                adapter = ArrayAdapter(this@MainActivity, android.R.layout.simple_spinner_dropdown_item,
                    locations.map { it.label })
                setSelection(locations.indexOfFirst { it.name == t.position }.coerceAtLeast(0))
            }
            box.addView(spinner)
            t to spinner
        }
        AlertDialog.Builder(this)
            .setTitle(s.dir.name)
            .setView(ScrollView(this).apply { addView(box) })
            .setPositiveButton("Save") { _, _ ->
                val notes = field.text.toString().trim()
                val feet = choices.associate { (t, spinner) ->
                    t.address to locations[spinner.selectedItemPosition].name
                }
                io.execute {
                    val r = runCatching { SessionJson.edit(s.dir, notes, feet) }
                    ui.post { r.onFailure { toast("Saving failed: ${it.message}") } }
                    refreshSessions()
                }
            }
            .setNegativeButton("Cancel", null)
            .show()
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

    private fun hasLocation() = checkSelfPermission(LOCATION) == PackageManager.PERMISSION_GRANTED

    /** REQUIRED, plus location if GPS recording is switched on. */
    private fun wantedPermissions() =
        if (prefs().getBoolean(PREF_GPS, true)) REQUIRED + LOCATION else REQUIRED

    private fun prefs() = getSharedPreferences("bladingtag", MODE_PRIVATE)

    private fun openAppSettings() {
        if (REQUIRED.any { shouldShowRequestPermissionRationale(it) } ||
            REQUIRED.none { checkSelfPermission(it) == PackageManager.PERMISSION_GRANTED }) {
            requestPermissions(wantedPermissions(), REQ_PERMISSIONS)
        } else {
            startActivity(Intent(android.provider.Settings.ACTION_APPLICATION_DETAILS_SETTINGS,
                android.net.Uri.fromParts("package", packageName, null)))
        }
    }

    companion object {
        private const val REQ_PERMISSIONS = 1
        private const val FRAME_MS = 50L
        private const val COLOR_BAD = 0xFFC62828.toInt()
        private val REQUIRED = arrayOf(
            Manifest.permission.BLUETOOTH_SCAN,
            Manifest.permission.BLUETOOTH_CONNECT,
            Manifest.permission.POST_NOTIFICATIONS,
        )
        /** Only needed for the optional GPS track. */
        private const val LOCATION = Manifest.permission.ACCESS_FINE_LOCATION
        private const val PREF_GPS = "record_gps"
        /** Addresses ticked for the last recording or standby. */
        private const val PREF_TAGS = "last_tags"
    }
}
