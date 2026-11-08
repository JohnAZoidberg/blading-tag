# blading-tag protocol

This file is the contract between the parts of the project. Each side can be
built and tested on its own against this spec.

1. **BLE protocol:** tag firmware ↔ Android app / laptop recorder.
2. **Session file format:** Android app / laptop recorder ↔ analysis.

```
 firmware/ ──(1) BLE──> android/, host/recorder ──(2) session files──> host/analysis
```

**Protocol version: 1.** It is reported in the advertising data and in the
Info characteristic. Change rules:

- **Compatible changes** (new fields appended to the end of a struct, new
  opcodes, new packet types, new error codes) keep the version number.
  Clients must ignore trailing bytes they don't know and packet types they
  don't know.
- **Breaking changes** bump the version. Clients refuse to record from a tag
  with a version they don't support and tell the user to update.

Conventions: all integers are **little-endian**. `uN`/`iN` are unsigned or
signed N-bit integers. Offsets are in bytes.

---

## 1. BLE protocol (tag ↔ app)

### 1.1 Advertising

The tag advertises when no central is connected: in the **Ready** state, and
in the **Recording** state after the link drops (see [1.9 Link loss](#19-link-loss)).
It supports one connection at a time.

| | |
|---|---|
| Interval | ~1 s in Ready, ~100 ms in Recording (faster reconnect); connectable, undirected |
| Address | static random, stable across reboots and updates; used to identify a tag |
| Pairing / bonding | none in v1 |

Advertising data (30 of 31 bytes):

| AD type | Contents |
|---|---|
| Flags | `0x06` (LE General Discoverable, BR/EDR not supported) |
| Complete list of 128-bit UUIDs | Blading service `b1ad0001-0e83-479a-bb08-19dab75f76db` |
| Manufacturer specific data | see below |

Manufacturer data (7 bytes after the AD header):

| Offset | Type | Field | Notes |
|---|---|---|---|
| 0 | u16 | company_id | `0xFFFF` (test / unassigned) |
| 2 | u8 | protocol_version | `1` |
| 3 | u8 | state | see [1.4 States](#14-states) |
| 4 | u8 | position | see [Position](#position) |
| 5 | u16 | battery_mv | coin-cell voltage in mV |

The scan response contains the **Complete Local Name** `BladeTag-XXXX`, where
`XXXX` is the last two bytes of the address in uppercase hex
(for example `BladeTag-0D57`).

Apps should filter scans on the service UUID, not the name.

### 1.2 GATT

All UUIDs share the base `b1adXXXX-0e83-479a-bb08-19dab75f76db`.

| UUID | Name | Properties | Max size |
|---|---|---|---|
| `b1ad0001-…` | **Blading service** (primary) | | |
| `b1ad0002-…` | Info | read | 12 B |
| `b1ad0003-…` | Control | write (with response) | 6 B |
| `b1ad0004-…` | Status | read, notify | 32 B |
| `b1ad0005-…` | Data | notify | MTU − 3 |

These standard services are also present:

- Generic Access / Generic Attribute.
- **SMP** (`8d53dc1d-1db7-4cd3-868b-8a527460aa84`) for firmware updates
  (mcumgr). It is always present.
- Device Information Service (`0x180A`): Firmware Revision String, Model Number
  String (`nRF54L15 Tag`).

#### Connection requirements

After connecting, the client must:

1. **Request an MTU of 247** or larger. The tag supports an ATT MTU of 247.
   Android 14+ requests 517 automatically; on older versions call
   `requestMtu(247)`.
2. Enable notifications on **Status** and **Data** by writing their CCCDs.
3. **Read Status** to find the current state. The tag may already be recording
   from an earlier connection (see [1.6 Link loss](#16-link-loss)).

The tag requests these connection parameters: 2M PHY, interval 30–50 ms,
latency 0, supervision timeout 4 s. The client may raise the priority
(Android `CONNECTION_PRIORITY_HIGH`) while recording, but doesn't have to.

### 1.3 Info (read)

Static capabilities. It doesn't change while connected.

| Offset | Type | Field | Notes |
|---|---|---|---|
| 0 | u8 | protocol_version | `1` |
| 1 | u8 | fw_major | |
| 2 | u8 | fw_minor | |
| 3 | u8 | fw_patch | |
| 4 | u16 | odr_mask | supported sample rates: bit0 25 Hz, bit1 50, bit2 100, bit3 200, bit4 400, bit5 800 |
| 6 | u8 | accel_range_mask | bit0 ±2 g, bit1 ±4 g, bit2 ±8 g, bit3 ±16 g |
| 7 | u8 | gyro_range_mask | bit0 ±125 °/s, bit1 ±250, bit2 ±500, bit3 ±1000, bit4 ±2000 |
| 8 | u32 | buffer_capacity | samples the tag can buffer during a link drop |

### 1.4 States

| Value | State | Meaning |
|---|---|---|
| 0 | READY | Idle. The IMU is suspended. The tag advertises when nobody is connected. |
| 1 | RECORDING | The IMU is sampling. Samples stream on Data, or are buffered while disconnected. |

**Asleep** (System OFF) is not visible over BLE. A sleeping tag doesn't
advertise. It wakes into READY on motion or a button press.

### Position

The tag stores where on the body it is worn, and keeps it across reboots and
updates. The label is what session.json and the tools use.

| Value | Label | Body location |
|---|---|---|
| 0 | `unknown` | unknown (default) |
| 1 | `left_foot` | left foot (skate) |
| 2 | `right_foot` | right foot (skate) |
| 3 | `left_wrist` | left wrist |
| 4 | `right_wrist` | right wrist |
| 5 | `left_pocket` | left pants pocket |
| 6 | `right_pocket` | right pants pocket |
| 7 | `backpack` | backpack |

Values 3–7 were added later. Older firmware answers SET_POSITION with them
with BAD_PARAM.

### 1.5 Control (write with response)

The first byte is the opcode. The tag answers through the **ATT write
response**:

- **Success** means the command was accepted.
- **An error** comes back as an ATT application error code (table below). On
  Android this is the `status` in `onCharacteristicWrite`.

| Opcode | Command | Parameters | Allowed in |
|---|---|---|---|
| `0x01` | START | `u16 odr_hz, u8 accel_range_g, u16 gyro_range_dps` (5 bytes, total length 6). Length 1 (opcode only) uses the defaults. A value of `0` uses the default for that field. Defaults: 200 Hz, ±16 g, ±2000 °/s. | READY |
| `0x02` | STOP | none | RECORDING (no-op success in READY) |
| `0x03` | IDENTIFY | `u8 seconds` (0 = stop). Blinks the LED. | any |
| `0x04` | SET_POSITION | `u8 position` (0–7, see [Position](#position)) | READY |
| `0x05` | SLEEP | none. Disconnects, then goes to System OFF. | READY |

Error codes:

| Code | Name | When |
|---|---|---|
| `0x80` | UNKNOWN_OPCODE | opcode not supported |
| `0x81` | BAD_LENGTH | wrong parameter length |
| `0x82` | BAD_PARAM | value not supported (check the Info masks) |
| `0x83` | BAD_STATE | not allowed in the current state; or START with Data notifications off, or with MTU < 64 |
| `0x84` | SENSOR_FAULT | the IMU didn't respond |

**START:**
1. The tag increments `session_id`, resets `seq` and `sample_index` to 0, and
   clears the counters.
2. It notifies Status with state RECORDING.
3. Data packets follow.

The tag drops the IMU's start-up samples itself. Sample 0 is the first valid
sample.

**STOP:**
1. The tag stops sampling.
2. It sends every sample still buffered on Data.
3. It notifies Status with state READY.

Notifications are delivered in order, so the READY Status marks the end of the
session's data.

Example START with the defaults written out:
`01 c8 00 10 d0 07` (200 Hz, ±16 g, ±2000 °/s).

### 1.6 Status (read, notify)

The tag notifies Status:

- when the state changes
- every 1 s while RECORDING and connected
- every 10 s while READY and connected

| Offset | Type | Field | Notes |
|---|---|---|---|
| 0 | u8 | state | 0 READY, 1 RECORDING |
| 1 | u8 | flags | bit0 IMU_FAULT, bit1 OVERFLOW (samples dropped this session), bit2 LOW_BATTERY |
| 2 | u16 | battery_mv | |
| 4 | u16 | session_id | increments on every START; persists across reboots |
| 6 | u16 | odr_hz | current or last setting |
| 8 | u8 | accel_range_g | 2, 4, 8 or 16 |
| 9 | u8 | position | see [Position](#position) |
| 10 | u16 | gyro_range_dps | 125 … 2000 |
| 12 | u32 | samples_recorded | this session |
| 16 | u32 | samples_dropped | this session (buffer overflow) |
| 20 | u32 | buffer_fill | samples in the buffer: not sent yet, or sent and not acknowledged |
| 24 | u64 | tag_time_us | tag clock "now", see [1.8 Time](#18-time) |

Example (RECORDING, 2950 mV, session 3, 200 Hz, ±16 g, ±2000 °/s,
6000 recorded, 38 buffered, t = 98.765432 s):

```
01 00 86 0b 03 00 c8 00 10 00 d0 07 70 17 00 00 00 00 00 00 26 00 00 00 78 0a e3 05 00 00 00 00
```

### 1.7 Data (notify)

Each notification is one packet: a 16-byte header plus `n` samples. The
`type` byte allows other packet types later. Clients skip types they don't
know.

Header:

| Offset | Type | Field | Notes |
|---|---|---|---|
| 0 | u8 | type | `0x01` = IMU6 |
| 1 | u8 | n | number of samples in this packet |
| 2 | u16 | seq | packet counter, 0 at START, wraps at 65536 |
| 4 | u32 | sample_index | index of the first sample, 0 at START |
| 8 | u64 | timestamp_us | tag clock when the first sample was taken |

Then `n` IMU6 samples, 12 bytes each:

| Offset | Type | Field |
|---|---|---|
| 0 | i16 | ax |
| 2 | i16 | ay |
| 4 | i16 | az |
| 6 | i16 | gx |
| 8 | i16 | gy |
| 10 | i16 | gz |

**Packet size:** the tag fills packets up to
`n = floor((MTU − 3 − 16) / 12)`. That's 19 samples (244 bytes) at MTU 247.
It may send smaller packets, for example when flushing on STOP. At 200 Hz this
is about 10.5 packets/s, or ~2.4 KB/s.

**Units:** the values are raw from the BMI270. Convert with the ranges from
Status:

- `accel_g = raw × accel_range_g / 32768`. At ±16 g that's 2048 LSB/g.
- `gyro_dps = raw × gyro_range_dps / 32768`. At ±2000 °/s that's 16.384 LSB/(°/s).

**Axes:** x/y/z are the BMI270 sensor axes as mounted on the Tag PCB. The tag
doesn't rotate them. The analysis works out the mounting orientation from
gravity. The mapping from sensor axes to PCB edges is documented in
`firmware/README.md` once measured.

**Gaps:**
- A jump in `sample_index` (it doesn't equal the previous
  `sample_index + n`) means the tag dropped samples because its buffer
  overflowed (`samples_dropped` counts them).
- A jump in `seq` means a packet was lost. BLE notifications are reliable
  within a connection, and the tag resends unacknowledged packets after a
  reconnect (see [1.9 Link loss](#19-link-loss)), so this shouldn't happen.
- A `seq` that repeats or goes back, with samples already received, is such
  a resend. Keep the first copy of each `sample_index`.

Example: type 1, 2 samples, seq 5, sample_index 1000,
timestamp 12.345678 s. Sample 0 is (0, 0, 2048, 0, 0, 0), i.e. 1 g on z at
rest. Sample 1 is (100, −100, 2048, 16, −16, 0).

```
01 02 05 00 e8 03 00 00 4e 61 bc 00 00 00 00 00
00 00 00 00 00 08 00 00 00 00 00 00
64 00 9c ff 00 08 10 00 f0 ff 00 00
```

### 1.8 Time

- The **tag clock** (`timestamp_us`, `tag_time_us`) counts microseconds since
  the tag booted (nRF54L GRTC). It never wraps in practice and resets on
  reboot. A new `session_id` starts after any reboot.
- The BMI270 sample clock is not exactly the nominal rate (up to about ±1 %).
  To get the **true sample rate**, fit `timestamp_us` against `sample_index`
  over the session. Don't assume `1 / odr_hz`.
- **Mapping to phone time** is the receiver's job. For each Data packet, log
  the phone receive time (§2). The analysis fits
  `phone_time ≈ a + b × tag_time` using the lower envelope of
  `(rx_time − timestamp)`, since the smallest delay is the truest. That gives
  ~ms accuracy and is how two tags are put on one timeline.

### 1.9 Link loss

- If the connection drops while RECORDING, the tag **keeps recording** into its
  buffer (`buffer_capacity` samples, ≥ 30 s at 200 Hz) and starts advertising
  again.
- When the buffer is full, the tag drops **new** samples and counts them in
  `samples_dropped`.
- The tag keeps each sample in the buffer until the Data packet carrying it
  has been acknowledged by the client's link layer, and keeps the last few
  acknowledged packets too (a controller can report a packet flushed at a
  link loss like an acknowledged one). Packets that were in flight when the
  link dropped may or may not have arrived. So on reconnect the tag sends
  again from the oldest packet it kept, with the same `seq` and
  `sample_index`. `timestamp_us` is interpolated again and can differ by a
  few µs, and with a different MTU the packets can be cut differently. So
  the client gets some samples twice (see [1.7 Data](#17-data-notify)).
- On reconnect, the client reads Status (state RECORDING) and re-enables
  notifications. The buffered packets then continue as described above.
  **The client must not send START again**; it would get BAD_STATE.
- After **5 minutes** disconnected, the tag stops recording on its own and
  goes to READY. The buffered data is discarded.

### 1.10 Firmware updates

Over the standard mcumgr SMP service, using the nRF Connect Device Manager app
or `make ota` (`host/ota.py`). No pairing is needed. The image is uploaded in
test mode. The new firmware confirms itself after BLE is up. Recording state
isn't kept across an update. The signed image must fit in a 664 KiB slot.

---

## 2. Session file format (recorder ↔ analysis)

The Android app and the laptop recorder write the same format, so the analysis
doesn't care which one recorded a session. The recorders store what the tag
sent **verbatim**, and leave decoding to the analysis. If the decoding has a
bug, old sessions can be re-analysed without loss.

A session is a folder, or a `.zip` of that folder for export:

```
session-20261107-142530/
  session.json        metadata
  tag-C0BB4AA70D57.bin  one per tag: raw notifications
  gps.csv             phone GPS fixes (Android only, may be missing)
```

### 2.1 Clocks

Every record carries **`elapsed_ns`**, a monotonic receiver clock that doesn't
jump when the wall clock is changed:

- Android: `SystemClock.elapsedRealtimeNanos()`; GPS: `Location.getElapsedRealtimeNanos()`.
- Laptop: `time.monotonic_ns()`.

`session.json` gives one pair `(start_elapsed_ns, start_utc)` to convert to
wall-clock time.

### 2.2 session.json

```json
{
  "format_version": 1,
  "recorder": { "name": "blading-tag-android", "version": "0.1.0" },
  "start_utc": "2026-11-07T14:25:30.123Z",
  "start_elapsed_ns": 123456789012,
  "end_elapsed_ns": 130656789012,
  "tags": [
    {
      "address": "C0:BB:4A:A7:0D:57",
      "position": "left_foot",
      "file": "tag-C0BB4AA70D57.bin",
      "info_hex": "01000100 3f000f1f 401f0000",
      "session_id": 3,
      "odr_hz": 200,
      "accel_range_g": 16,
      "gyro_range_dps": 2000,
      "simulated": false
    }
  ],
  "notes": "",
  "activity": "walking",
  "gear": "Brooks Ghost 15"
}
```

- `position` is a label from [Position](#position). Sessions recorded before
  body locations existed say `"left"` or `"right"`; readers take those as
  `"left_foot"` and `"right_foot"`.
- `notes` is a free-text description, empty by default. The Android app lets
  you edit it, and the foot/body location, after recording.
- `info_hex` is the raw Info characteristic. Spaces are optional.
- `activity` is what the session is: `"skating"`, `"walking"` or
  `"cycling"`. Missing, or a label the reader doesn't know, means
  `"unknown"` (every session from before the field). It belongs to the
  session, not the tag; nothing is sent to the tag.
- `gear` is free text naming the shoes, skates or bike, e.g.
  `"Brooks Ghost 15"`; missing or `""` means not given. Readers compare it
  as written, after trimming spaces. Both fields can be changed after
  recording (`bladetag label`, the app's edit dialog).
- `simulated` is `true` for fake data from a simulator (the Android app's
  simulated tag, `bladetag record fake`, `bladetag simulate`), so test data
  isn't mistaken for a real activity. A session is simulated if any of its
  tags is. Sessions written before the field have no `simulated`; readers
  treat them as simulated if a tag has a simulator-only address
  (`02:00:00:00:00:01` on Android, `FA:KE:…` on the host) or the folder has
  a `sim_truth.json` (written by `bladetag simulate`, which used the real
  tag's address).
- Readers ignore fields they don't know.

### 2.3 tag-ADDRESS.bin

The file is a sequence of records, with no file header:

| Offset | Type | Field | Notes |
|---|---|---|---|
| 0 | u64 | elapsed_ns | receive time, see [2.1 Clocks](#21-clocks) |
| 8 | u8 | kind | 1 = Data notification, 2 = Status notification or read |
| 9 | u16 | length | payload length |
| 11 | bytes | payload | exactly as received from the tag |

Writers log every Data and Status notification. They also log a Status read
right after each (re)connect. Disconnects show up as gaps in the timeline.

Example: the record header for the Data example above, received at
`elapsed_ns = 123456789012`, is `14 1a 99 be 1c 00 00 00 01 28 00`, followed by
the 40 payload bytes.

### 2.4 gps.csv

One row per location fix, with a header row:

```
elapsed_ns,utc_ms,lat,lon,alt_m,speed_mps,bearing_deg,h_acc_m,speed_acc_mps
```

Fields the phone didn't provide are left empty. `speed_mps` is the GNSS
Doppler speed from `Location.getSpeed()`, not derived from positions.
