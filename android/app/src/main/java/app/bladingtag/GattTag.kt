package app.bladingtag

import android.annotation.SuppressLint
import android.bluetooth.BluetoothDevice
import android.bluetooth.BluetoothGatt
import android.bluetooth.BluetoothGattCallback
import android.bluetooth.BluetoothGattCharacteristic
import android.bluetooth.BluetoothGattDescriptor
import android.bluetooth.BluetoothManager
import android.bluetooth.BluetoothProfile
import android.content.Context
import android.os.Handler
import android.os.HandlerThread
import android.os.SystemClock
import android.util.Log

/**
 * A real tag over BLE, following PROTOCOL.md §1.2 (connection setup), §1.5
 * (START/STOP) and §1.9 (link loss).
 *
 * Everything runs on one HandlerThread per tag: connectGatt is given its
 * handler, so GATT callbacks arrive there too and no state is shared with
 * other threads except the counters in [TagLink]. Android allows one
 * outstanding GATT operation at a time, hence the [ops] queue.
 *
 * Two modes:
 * - recording ([oneShot] == null): START after connecting, reconnect on
 *   link loss until [stop].
 * - one-shot: connect, write [oneShot] (IDENTIFY, SET_POSITION, ...),
 *   disconnect, report the result via [onOneShotDone]. Nothing is logged.
 */
@SuppressLint("MissingPermission") // callers check BLUETOOTH_CONNECT
class GattTag(
    private val context: Context,
    address: String,
    listener: Listener,
    private val startCommand: ByteArray = Protocol.start(),
    private val oneShot: ByteArray? = null,
    private val onOneShotDone: ((error: String?) -> Unit)? = null,
) : TagLink(address, listener) {

    private val thread = HandlerThread("tag-$address").apply { start() }
    private val handler = Handler(thread.looper)
    private val device: BluetoothDevice =
        context.getSystemService(BluetoothManager::class.java).adapter.getRemoteDevice(address)

    private var gatt: BluetoothGatt? = null
    private var control: BluetoothGattCharacteristic? = null
    private val ops = ArrayDeque<() -> Boolean>()
    private var busy = false

    private var running = false
    private var everStarted = false
    private var stopDone: (() -> Unit)? = null
    private var finished = false

    override fun start() {
        handler.post {
            running = oneShot == null
            connect(autoConnect = false)
        }
    }

    override fun stop(done: () -> Unit) {
        handler.post { stopOnThread(done) }
    }

    private fun stopOnThread(done: () -> Unit) {
        if (finished) { done(); return }
        running = false
        stopDone = done
        if (gatt != null && control != null) {
            write(Protocol.stop())
            // STOP normally ends with a READY Status once the buffer is
            // flushed; don't hang forever if it never comes.
            handler.postDelayed(::finish, STOP_TIMEOUT_MS)
        } else {
            finish()
        }
    }

    override fun command(bytes: ByteArray) {
        handler.post { if (control != null) write(bytes) }
    }

    private fun connect(autoConnect: Boolean) {
        Log.i(TAG, "$address: connect(autoConnect=$autoConnect)")
        gatt = device.connectGatt(context, autoConnect, callback, BluetoothDevice.TRANSPORT_LE,
            BluetoothDevice.PHY_LE_1M_MASK or BluetoothDevice.PHY_LE_2M_MASK, handler)
        if (!autoConnect) handler.postDelayed(connectTimeout, CONNECT_TIMEOUT_MS)
    }

    /** A direct connect gives up after ~30 s on some phones and not at all on
     *  others; fall back to a background (autoConnect) connection. */
    private val connectTimeout = Runnable {
        if (!connected && running) {
            Log.i(TAG, "$address: direct connect timed out")
            gatt?.close()
            connect(autoConnect = true)
        } else if (!connected && oneShot != null) {
            oneShotResult("tag not found")
        }
    }

    private fun finish() {
        if (finished) return
        finished = true
        handler.removeCallbacksAndMessages(null)
        running = false
        gatt?.disconnect()
        gatt?.close()
        gatt = null
        control = null
        connected = false
        thread.quitSafely()
        stopDone?.invoke()
        stopDone = null
    }

    private fun oneShotResult(err: String?) {
        if (finished) return
        Log.i(TAG, "$address: one-shot done, error=$err")
        val cb = onOneShotDone
        finish()
        cb?.invoke(err)
    }

    // ---- operation queue ----------------------------------------------------

    private fun enqueue(op: () -> Boolean) {
        ops.addLast(op)
        next()
    }

    private fun next() {
        if (busy) return
        while (ops.isNotEmpty()) {
            val op = ops.removeFirst()
            if (op()) { busy = true; return }
            Log.w(TAG, "$address: GATT operation could not be issued")
        }
    }

    private fun done() {
        busy = false
        next()
    }

    private fun write(bytes: ByteArray) = enqueue {
        val g = gatt; val c = control
        g != null && c != null && g.writeCharacteristic(c, bytes,
            BluetoothGattCharacteristic.WRITE_TYPE_DEFAULT) == BluetoothGatt.GATT_SUCCESS
    }

    // ---- GATT callbacks (on [handler]) --------------------------------------

    private val callback = object : BluetoothGattCallback() {

        override fun onConnectionStateChange(g: BluetoothGatt, status: Int, newState: Int) {
            if (g != gatt) { g.close(); return }
            if (newState == BluetoothProfile.STATE_CONNECTED) {
                Log.i(TAG, "$address: connected")
                handler.removeCallbacks(connectTimeout)
                connected = true
                ops.clear(); busy = false
                g.discoverServices()
                return
            }
            Log.i(TAG, "$address: disconnected, status=$status")
            handler.removeCallbacks(connectTimeout)
            val wasConnected = connected
            connected = false
            control = null
            ops.clear(); busy = false
            g.close()
            gatt = null
            when {
                stopDone != null -> finish()
                oneShot != null -> oneShotResult("disconnected (status $status)")
                running -> {
                    // §1.9: the tag keeps recording into its buffer; wait for
                    // it to advertise again. autoConnect doesn't time out.
                    if (wasConnected) reconnects++
                    handler.postDelayed({ if (running) connect(autoConnect = true) }, 500)
                }
            }
        }

        override fun onServicesDiscovered(g: BluetoothGatt, status: Int) {
            val svc = g.getService(Protocol.SERVICE)
            val info = svc?.getCharacteristic(Protocol.INFO)
            val ctl = svc?.getCharacteristic(Protocol.CONTROL)
            val st = svc?.getCharacteristic(Protocol.STATUS)
            val data = svc?.getCharacteristic(Protocol.DATA)
            if (status != BluetoothGatt.GATT_SUCCESS || info == null || ctl == null || st == null || data == null) {
                error = "no Blading service on this device"
                Log.w(TAG, "$address: $error")
                if (oneShot != null) oneShotResult(error) else g.disconnect()
                return
            }
            control = ctl
            // §1.2: MTU, then notifications on Status and Data, then read Status.
            enqueue { g.requestMtu(Protocol.MTU) }
            enqueue { g.readCharacteristic(info) }
            if (oneShot == null) {
                enqueue { enableNotify(g, st) }
                enqueue { enableNotify(g, data) }
            }
            enqueue { g.readCharacteristic(st) }
        }

        override fun onMtuChanged(g: BluetoothGatt, mtu: Int, status: Int) {
            this@GattTag.mtu = mtu
            Log.i(TAG, "$address: mtu=$mtu")
            done()
        }

        override fun onCharacteristicRead(g: BluetoothGatt, c: BluetoothGattCharacteristic,
                                          value: ByteArray, status: Int) {
            val now = SystemClock.elapsedRealtimeNanos()
            if (status == BluetoothGatt.GATT_SUCCESS) when (c.uuid) {
                Protocol.INFO -> handleInfo(value).also { checkVersion() }
                Protocol.STATUS -> {
                    if (oneShot == null) handleStatus(now, value)
                    else Protocol.parseStatus(value)?.let { this@GattTag.status = it }
                    afterConnect()
                }
            }
            done()
        }

        override fun onDescriptorWrite(g: BluetoothGatt, d: BluetoothGattDescriptor, status: Int) {
            if (status != BluetoothGatt.GATT_SUCCESS) error = "enabling notifications failed ($status)"
            done()
        }

        override fun onCharacteristicWrite(g: BluetoothGatt, c: BluetoothGattCharacteristic, status: Int) {
            val err = if (status == BluetoothGatt.GATT_SUCCESS) null else Protocol.errorName(status)
            if (err != null) {
                error = err
                Log.w(TAG, "$address: control write failed: $err")
            }
            if (oneShot != null) {
                oneShotResult(err)
                return
            }
            // STOP while already READY is a no-op success with no Status change.
            if (stopDone != null && this@GattTag.status?.state == Protocol.STATE_READY) finish()
            done()
        }

        override fun onCharacteristicChanged(g: BluetoothGatt, c: BluetoothGattCharacteristic, value: ByteArray) {
            val now = SystemClock.elapsedRealtimeNanos()
            when (c.uuid) {
                Protocol.DATA -> handleData(now, value)
                Protocol.STATUS -> {
                    handleStatus(now, value)
                    if (stopDone != null && status?.state == Protocol.STATE_READY) finish()
                }
            }
        }
    }

    private fun checkVersion() {
        val v = info?.version ?: return
        if (v != Protocol.VERSION) error = "tag speaks protocol v$v, this app v${Protocol.VERSION}: update"
    }

    private fun enableNotify(g: BluetoothGatt, c: BluetoothGattCharacteristic): Boolean {
        g.setCharacteristicNotification(c, true)
        val d = c.getDescriptor(Protocol.CCCD) ?: return false
        return g.writeDescriptor(d, BluetoothGattDescriptor.ENABLE_NOTIFICATION_VALUE) ==
            BluetoothGatt.GATT_SUCCESS
    }

    /** Runs after the Status read that follows every (re)connect. */
    private fun afterConnect() {
        if (oneShot != null) {
            write(oneShot)
            return
        }
        if (info?.version?.let { it != Protocol.VERSION } == true) return
        val s = status ?: return
        when {
            stopDone != null -> write(Protocol.stop())
            !running -> {}
            s.state == Protocol.STATE_RECORDING -> {
                // Either our own session after a reconnect (§1.9: don't START
                // again), or the tag was started with its button.
                everStarted = true
            }
            s.state == Protocol.STATE_READY -> {
                // Not recording: a fresh start, or the tag gave up after the
                // 5-minute link-loss timeout or rebooted. Start (again) rather
                // than lose the rest of a long session; the new session_id
                // shows up in the logged Status records.
                if (everStarted) restarts++
                everStarted = true
                resetSequence()
                write(startCommand)
            }
        }
    }

    companion object {
        private const val TAG = "BladingTag"
        private const val CONNECT_TIMEOUT_MS = 15_000L
        private const val STOP_TIMEOUT_MS = 5_000L
    }
}
