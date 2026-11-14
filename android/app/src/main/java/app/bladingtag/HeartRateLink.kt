package app.bladingtag

import android.annotation.SuppressLint
import android.bluetooth.*
import android.bluetooth.le.*
import android.content.Context
import android.os.Handler
import android.os.Looper
import android.os.ParcelUuid
import android.os.SystemClock
import android.util.Log

/** All state transitions run on the main thread. Each failed attempt closes its GATT
 * and scans again; generation checks discard callbacks from previous connections. */
@SuppressLint("MissingPermission")
class HeartRateLink(private val context: Context, private val address: String?,
                    private val onDevice: (HeartRateMeta) -> Unit,
                    private val onPacket: (Long, ByteArray) -> Unit) {
    private val main = Handler(Looper.getMainLooper())
    private var selectedAddress = address
    private var active = false
    private var scanning = false
    private var gatt: BluetoothGatt? = null
    private val adapter get() = context.getSystemService(BluetoothManager::class.java).adapter
    var state = "Searching for HRM…"
        private set
    var measurement: HeartRate.Measurement? = null
        private set
    private var lastNs = 0L
    var reconnects = 0
        private set
    private var joined = false
    fun display(): String {
        val m = measurement
        val fresh = SystemClock.elapsedRealtimeNanos() - lastNs < 5_000_000_000L
        return if (m != null && fresh && m.bpm > 0 && m.contactDetected != false)
            "Heart rate  ${m.bpm} bpm" else "Heart rate  – · $state"
    }
    fun start() { active = true; search() }
    fun stop() {
        active = false
        main.removeCallbacksAndMessages(null)
        stopScan()
        val old = gatt; gatt = null
        old?.disconnect(); old?.close()
        measurement = null
    }
    private val retry = Runnable { search() }
    private val timeout = Runnable { failed("HRM timed out") }
    private fun failed(reason: String) {
        if (!active) return
        Log.w("HeartRateLink", "$reason; retrying")
        state = "$reason; reconnecting…"
        measurement = null
        selectedAddress = address
        main.removeCallbacks(timeout)
        stopScan()
        val old = gatt; gatt = null
        old?.disconnect(); old?.close()
        main.removeCallbacks(retry)
        main.postDelayed(retry, 10_000)
    }
    private fun search() {
        if (!active) return
        try {
            val scanner = adapter?.bluetoothLeScanner
            if (scanner == null) { failed("Bluetooth unavailable"); return }
            state = "Searching for HRM…"
            val filter = ScanFilter.Builder().apply {
                if (selectedAddress != null) setDeviceAddress(selectedAddress!!) else setServiceUuid(ParcelUuid(HeartRate.SERVICE))
            }.build()
            scanner.startScan(listOf(filter), ScanSettings.Builder()
                .setScanMode(ScanSettings.SCAN_MODE_LOW_POWER).build(), scan)
            scanning = true
            main.postDelayed(timeout, 30_000)
        } catch (e: RuntimeException) { failed("HRM scan failed: ${e.message}") }
    }
    private fun stopScan() {
        if (scanning) runCatching { adapter?.bluetoothLeScanner?.stopScan(scan) }
        scanning = false
    }
    private val scan = object : ScanCallback() {
        override fun onScanFailed(errorCode: Int) { failed("HRM scan failed ($errorCode)") }
        override fun onScanResult(callbackType: Int, r: ScanResult) {
            if (!active || !scanning) return
            stopScan()
            main.removeCallbacks(timeout)
            selectedAddress = r.device.address
            onDevice(HeartRateMeta(r.device.address, r.scanRecord?.deviceName ?: r.device.name ?: "Heart-rate monitor"))
            state = "Connecting to HRM…"
            try {
                gatt = r.device.connectGatt(context, false, callback, BluetoothDevice.TRANSPORT_LE)
                main.postDelayed(timeout, 30_000)
            } catch (e: RuntimeException) { failed("HRM connection failed: ${e.message}") }
        }
    }
    private fun dispatch(g: BluetoothGatt, action: () -> Unit) {
        main.post { if (active && gatt === g) action() }
    }
    private val callback = object : BluetoothGattCallback() {
        override fun onConnectionStateChange(g: BluetoothGatt, status: Int, newState: Int) = dispatch(g) {
            if (status != BluetoothGatt.GATT_SUCCESS || newState == BluetoothProfile.STATE_DISCONNECTED)
                failed("HRM disconnected ($status)")
            else if (newState == BluetoothProfile.STATE_CONNECTED) {
                state = "Setting up HRM…"
                if (!g.discoverServices()) failed("HRM discovery failed")
            }
        }
        override fun onServicesDiscovered(g: BluetoothGatt, status: Int) = dispatch(g) {
            val c = g.getService(HeartRate.SERVICE)?.getCharacteristic(HeartRate.MEASUREMENT)
            val d = c?.getDescriptor(HeartRate.CCCD)
            if (status != BluetoothGatt.GATT_SUCCESS || c == null || d == null ||
                !g.setCharacteristicNotification(c, true) ||
                g.writeDescriptor(d, BluetoothGattDescriptor.ENABLE_NOTIFICATION_VALUE) != BluetoothStatusCodes.SUCCESS)
                failed("HRM subscription failed")
        }
        override fun onDescriptorWrite(g: BluetoothGatt, d: BluetoothGattDescriptor, status: Int) = dispatch(g) {
            if (status != BluetoothGatt.GATT_SUCCESS) failed("HRM subscription failed ($status)")
            else {
                main.removeCallbacks(timeout)
                if (joined) reconnects++
                joined = true
                state = "Waiting for HRM measurement…"
                Log.i("HeartRateLink", "subscribed; reconnects=$reconnects")
                main.postDelayed(timeout, 30_000)
            }
        }
        override fun onCharacteristicChanged(g: BluetoothGatt, c: BluetoothGattCharacteristic, value: ByteArray) {
            val ns = SystemClock.elapsedRealtimeNanos()
            val raw = value.copyOf()
            dispatch(g) {
                if (c.uuid == HeartRate.MEASUREMENT) {
                    onPacket(ns, raw)
                    measurement = runCatching { HeartRate.decode(raw) }.getOrNull()
                    lastNs = ns
                    state = when {
                        measurement == null -> "Invalid HRM measurement"
                        measurement?.contactDetected == false -> "Check HRM contact"
                        measurement?.bpm == 0 -> "HRM measurement unavailable"
                        else -> "HRM reading stale"
                    }
                    main.removeCallbacks(timeout)
                    main.postDelayed(timeout, 30_000)
                }
            }
        }
    }
}
