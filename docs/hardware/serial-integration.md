# WiSense-Twin — Section 15: Real Hardware Integration (Serial CSI Source)

## 1. Overview & System Role

Section 15 closes the loop between the embedded ESP32 firmware ([Section 13 `tx_beacon`](./transmitter-firmware.md) and [Section 14 `rx_csi_collector`](./receiver-firmware.md)) and the Python analytics backend ([Sections 1–12](../../backend_pipeline/)).

```
+------------------------+          802.11 CSI Packets (100 pps)
| ESP32 Transmitter      | ---------------------------------------------> +------------------------+
| tx_beacon (Sec 13)     |                                                | ESP32 Receiver         |
| MAC: 1a:00:00:00:00:00 |                                                | rx_csi_collector (14)  |
+------------------------+                                                +-----------+------------+
                                                                                      |
                                                          Binary Framed UART @ 921600 | (Magic + Header + Payload + CRC)
                                                                                      v
+--------------------------------------------------------------------------------------------------+
| Python Backend Pipeline (wisense_backend)                                                       |
|                                                                                                  |
|   +------------------------------------+                                                         |
|   | SerialCSISource (Section 15)       | <--- Implements CSIRecordSource protocol                |
|   | - CSIFrameParser streaming resync  |                                                         |
|   | - time.time() host epoch timestamp |                                                         |
|   | - math.hypot(I, Q) amplitudes      |                                                         |
|   +-----------------+------------------+                                                         |
|                     |                                                                            |
|                     v CSIRecord stream                                                           |
|   +------------------------------------------------------------------------------------------+   |
|   | Downstream Generic Pipeline (ZERO CODE MODIFICATIONS REQUIRED)                           |   |
|   | - Preprocessing (Hampel & Moving Average, Sec 4)                                         |   |
|   | - Feature Extraction (Sliding Window Variance, Sec 5)                                    |   |
|   | - Motion Detection (Hysteresis Thresholds, Sec 6)                                        |   |
|   | - Zone Classification (Nearest Reference Signatures, Sec 8)                              |   |
|   | - Telemetry Persistence (SQLite Manager, Sec 9)                                          |   |
|   | - Realtime Publishing (WebSocket Server, Sec 10)                                         |   |
|   | - Health Telemetry (CSI Staleness Probes, Sec 11)                                        |   |
|   | - Developer Dashboard (Dynamic Zone Visualizer, Sec 12)                                  |   |
|   +------------------------------------------------------------------------------------------+   |
+--------------------------------------------------------------------------------------------------+
```

### The Interface Genericity Payoff
From Section 1 onward, every architectural decision in this project enforced two strict separation rules:
1. **Zero Ground-Truth Leakage into `CSIRecord`**: Records never contain synthetic tags, scenario events, or simulated zone labels.
2. **Generic Interface Consumption**: Every downstream module consumes frames exclusively via the `CSIRecordSource` protocol (`open()`, `close()`, `is_open`, `read_record()`, `records()`).

Because `SerialCSISource` implements the exact `CSIRecordSource` protocol and maps raw binary frames into canonical `CSIRecord` instances, the entire downstream analytical pipeline (Sections 4 through 12) runs unmodified against physical hardware data.

---

## 2. Configuration Architecture

Hardware serial ingestion is configured via `SerialSourceConfig` and integrated directly into `AppConfig`:

### 2.1 Configuration Schema

| Parameter | Type | Default | Constraint / Rationale |
| :--- | :---: | :---: | :--- |
| `port` | `str` | *None (Required)* | Serial port identifier (e.g. `COM4` or `/dev/ttyUSB0`). **No hardcoded defaults** permitted per project constraints. |
| `baud_rate` | `int` | `921600` | UART transfer speed. Defaults to `921600` to match `CONFIG_RX_CSI_UART_BAUDRATE` in `rx_csi_collector`. |
| `timeout` | `float` | `1.0` | Maximum read/resync blocking timeout in seconds. Must be positive. |

### 2.2 Application Wiring (`system_config.json`)

To enable physical hardware mode, configure `system_config.json` with `csi_source_mode = "hardware"`:

```json
{
  "websocket_host": "127.0.0.1",
  "websocket_port": 8765,
  "database_path": "data/wisense.db",
  "log_level": "INFO",
  "log_format": "text",
  "csi_source_mode": "hardware",
  "serial_port": "COM4",
  "serial_baud_rate": 921600
}
```

`Application._create_source()` checks `csi_source_mode == "hardware"`, validates that `serial_port` is non-empty, and instantiates `SerialCSISource(SerialSourceConfig(port=..., baud_rate=...))`. The legacy `NotImplementedError` placeholder is completely eliminated.

---

## 3. Binary Framing Protocol & Stream Parsing

`SerialCSISource` uses `CSIFrameParser` ([`frame_parser.py`](../../backend_pipeline/src/wisense_backend/ingestion/frame_parser.py)) to process incoming bytes from the ESP32 receiver.

### 3.1 Wire Protocol Structure

Every CSI frame emitted by `rx_csi_collector` adheres to the 4-part binary structure:

```text
+---------------------+-------------------+---------------------+------------------+
| Magic Sync Marker   | Fixed Header      | Raw CSI Payload     | Trailer & CRC    |
| 4 Bytes             | 24 Bytes Body     | N Bytes (<= 384)    | 4 Bytes          |
| 0xAA 0x55 0x57 0x53 | 28 Bytes Total    | Signed 8-bit I/Q    | CRC-16 + 0x55 0xAA
+---------------------+-------------------+---------------------+------------------+
```

1. **Magic Sync Marker (4 Bytes)**: `0xAA, 0x55, 0x57, 0x53` (`\xaa\x55WS`).
2. **Fixed Header Body (24 Bytes)**: Little-endian `<HHIIIbBbBbb2s`:
   - `header_len` (`uint16`, must be 28)
   - `payload_len` (`uint16`, $N \le 384$)
   - `seq_num` (`uint32`)
   - `rx_timestamp_us` (`uint32`, local receiver microsecond clock)
   - `sender_ts_ms` (`uint32`, transmitter beacon millisecond clock)
   - `rssi` (`int8`, dBm)
   - `channel` (`uint8`)
   - `noise_floor` (`int8`, dBm)
   - `first_word_invalid` (`uint8`)
   - `sig_mode` (`int8`)
   - `mcs` (`int8`)
   - `reserved` (`2 bytes`)
3. **Raw CSI Payload ($N$ Bytes)**: Interleaved signed 8-bit integers (`[imag_0, real_0, imag_1, real_1, ...]`).
4. **Trailer (4 Bytes)**: Little-endian 16-bit CRC-16-CCITT followed by `0x55, 0xAA`.

---

## 4. Resilience, Resynchronization & Corrupted Frame Handling

### 4.1 The Justification: The Real `walking_01.txt` Incident
During exploratory data collection, hardware logging revealed an 11.9-second transmission stall that produced a truncated, torn record. When UART buffers overrun or transmission stalls, frames can be fragmented or torn mid-payload. A naive parser expecting fixed offsets crashes, desynchronizes, or permanently returns garbage.

### 4.2 Streaming Resynchronization Algorithm

`CSIFrameParser` implements a self-healing sliding window:

1. **Magic Seek**: `buffer.find(MAGIC)` scans forward. Any non-binary bytes preceding magic (such as ESP-IDF boot logs or `[RX_STAT]` lines) are safely extracted and discarded. Because `0xAA` (170) is outside the 7-bit ASCII range (`0x20`–`0x7E`, `\r`, `\n`), ASCII logs never cause false magic matches.
2. **Header Sanity Validation**: Checks `header_len == 28`, `0 < payload_len <= 384`, and `payload_len % 2 == 0`. If any check fails, the 4 magic bytes are discarded (`del buffer[:4]`), `corrupt_frame_count` is incremented, and seeking resumes immediately.
3. **Early Torn-Frame Detection**: If a subsequent `MAGIC` is detected in the buffer before `total_len` has accumulated, and that subsequent magic is followed by a verified `header_len == 28`, the current frame is confirmed torn. The parser increments `corrupt_frame_count`, drops the torn prefix up to the next magic, and immediately parses the following valid frame.
4. **CRC-16 & Trailer Verification**: When `total_len` bytes have accumulated:
   - Validates `trailer == 0x55, 0xAA`.
   - Computes CRC-16-CCITT over `header + payload` via `binascii.crc_hqx(header_and_payload, 0xFFFF)`.
   - If CRC or trailer mismatches, `corrupt_frame_count` is incremented, `buffer[:4]` is discarded, and resynchronization seeks the next magic marker.
5. **Loss Visibility**: Frames that fail CRC or are torn are skipped and never returned to the application. However, every corrupt frame increments `source.corrupt_frame_count`, guaranteeing visible loss metrics.

---

## 5. Contract Conformance & Field Transformations

### 5.1 Host Epoch Timestamp vs. Device Boot Clocks
- **The Contract**: Project constraints mandate that `CSIRecord.timestamp` must strictly represent UTC Unix epoch seconds (`time.time()`).
- **Implementation**: The host captures `time.time()` at the exact moment a frame is received and verified.
- **Diagnostics Isolation**: Device boot clocks (`rx_timestamp_us` and `sender_ts_ms`) are retained on `frame.header` and accessible via `source.last_frame.header` for hardware diagnostics, but are never placed into `CSIRecord.timestamp`.

### 5.2 Subcarrier Amplitudes & Session Consistency
- **Amplitude Calculation**: For each signed 8-bit I/Q pair $(I, Q)$, amplitude is computed using:
  $$\text{amplitude} = \sqrt{I^2 + Q^2} = \text{math.hypot}(I, Q)$$
- **Dynamic Derivation**: Subcarrier count is derived per record from the header:
  $$\text{subcarrier\_count} = \frac{\text{payload\_len}}{2}$$
  (e.g. 192 for 384-byte HT40; 64 for 128-byte HT20).
- **Session Consistency Enforcement**: When `SerialCSISource` opens, the subcarrier count of the first valid frame establishes the session expectation. If any subsequent frame in the same session contains a different subcarrier count, `read_record()` raises a `ValueError("Inconsistent subcarrier count mid-stream...")`.

### 5.3 Phases
- `phases` is set to `None` by default, strictly maintaining the project constraint that phase unwrapping is experimental and never a hard dependency for core features.

### 5.4 Timeout Contract & Scoped Deviation (`read_record(timeout=None)`)
- **Protocol Contract**: The generic `CSIRecordSource` protocol docstring (Section 1) states that `timeout=None` blocks indefinitely until a record is available or the stream ends.
- **Intentional Scoped Deviation**: `SerialCSISource.read_record(timeout=None)` intentionally falls back to the configured finite timeout (`config.timeout`, default 1.0 s) rather than blocking indefinitely.
- **Rationale**: On real hardware serial ports, a disconnected, unpowered, or paused transmitter does not emit EOF on an open serial handle; an unbounded blocking read would hang the caller thread forever. Bounded waiting ensures that stalled or dead hardware streams reliably surface as timeout events (`None`), allowing upstream coordinators (like `Application` or health monitoring) to observe the stall, inspect diagnostic counters, or cleanly respond to shutdown signals.

---

## 6. Scope Boundaries

### Explicitly Out of Scope
1. **Automatic Serial Reconnection**: In line with the established deferred-scope pattern (e.g. Section 11 observation-only health monitor), automated reconnection, port re-enumeration, and hardware recovery are deferred. `SerialCSISource` provides robust framing parsing and resynchronization over torn bytes, but does not implement auto-reconnect loops.
2. **UE5 Digital Twin Integration**: Handled in Sections 16–20.
3. **Application End-to-End Orchestration**: Full concurrent background threading for WebSocket, SQLite, and health monitoring remains deferred to Section 21.
4. **Physical Calibration Tuning**: Empirical calibration of motion thresholds and zone signatures against live multipath hardware is deferred to post-integration testing.

---

## 7. Verification & Empirical Results

### 7.1 Automated Unit & Integration Suite (`FakeSerial`)
Because CI environments do not have physical ESP32 transceivers connected, all protocol operations are verified via a duck-typed `FakeSerial` mock backend:

- **Protocol Conformance**: `isinstance(SerialCSISource(...), CSIRecordSource)` verified.
- **Clean Ingestion**: 5 sequential frames parsed with exact amplitude values $\sqrt{3^2 + 4^2} = 5.0$, $\sqrt{(-5)^2 + 12^2} = 13.0$, matching sequence numbers, and valid host timestamps.
- **Corrupted Frame Resync**: Bad CRC frame injected between valid frames; source correctly skips bad frame, increments `corrupt_frame_count == 1`, and retrieves subsequent frame.
- **Torn Frame Resync**: Truncated frame (simulating `walking_01.txt` stall) injected; parser detects subsequent magic, resyncs, and preserves stream integrity.
- **Subcarrier Mismatch**: Session subcarrier count mismatch mid-stream raises `ValueError`.
- **Application Run Integration**: `Application(config=AppConfig(csi_source_mode="hardware", serial_port="COM4"))` successfully runs the ingestion loop with `FakeSerial` and shuts down cleanly.

All 222 tests in the project test suite pass with zero regressions.

### 7.2 Hardware Verification Check
At the time of this section's implementation, the host machine's serial ports were polled using `serial.tools.list_ports.comports()`. Physical ESP32 hardware boards (`COM3`/`COM4`) were powered off/disconnected (`comports() == []`). Physical end-to-end transceiver verification was previously completed in Section 14 (96.3 pps, 0/5721 corrupt frames over 59.38s via `scripts/raw_csi_monitor.py`), and the wire protocol parsing logic in Section 15 is directly ported and extracted from that verified run.
