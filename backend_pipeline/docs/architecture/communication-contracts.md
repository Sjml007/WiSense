# Communication Contracts & WebSocket Specification

## 1. Overview

WiSense-Twin broadcasts real-time RF sensing decisions to downstream visualizers, primarily an Unreal Engine 5 (UE5) digital twin, web monitoring dashboards, and diagnostic test harnesses. Communication occurs via a lightweight, unidirectional WebSocket server streaming JSON-serialized state messages.

The transport layer is defined by the `WebSocketPublisher` protocol in [`contracts/websocket_publisher.py`](file:///c:/Users/mohan/Desktop/Academic/Software/WiSense%20Twin/backend_pipeline/src/wisense_backend/contracts/websocket_publisher.py), with the concrete implementation provided by `WebSocketStatePublisher` in [`communication/websocket_server.py`](file:///c:/Users/mohan/Desktop/Academic/Software/WiSense%20Twin/backend_pipeline/src/wisense_backend/communication/websocket_server.py).

---

## 2. Wire Protocol Schema (`StateMessage`)

Each broadcast payload represents the spatial state derived from the pipeline's current decision epoch.

### Schema Fields

| Field Name | Type | Nullable | Description | Invariant Rules |
| :--- | :--- | :--- | :--- | :--- |
| `schema_version` | Integer | No | Monotonically increasing protocol schema version. | Must be $\ge 1$ (currently `1`). |
| `timestamp` | Float | No | UTC Unix epoch timestamp (seconds) of the classification window end (latest frame time). Represents the arrival time of the final CSI packet closing the analysis window ($t_{\text{frame}}$). Aligning the timestamp with window-end (frame) time rather than a window midpoint or start ensures accurate end-to-end computational latency tracking ($t_{\text{host}} - t_{\text{record}}$) and allows downstream consumers (UE5 watchdog) to evaluate link staleness directly against physical transmission time. | Must be $> 0.0$. |
| `zone_id` | Integer | **Yes** | 1-indexed identifier of the predicted active zone ($1 \le \text{zone\_id} \le \text{zone\_count}$). | Must be `null` if `is_motion` is `false` or if ambiguous. |
| `is_motion` | Boolean | No | Instantaneous motion indicator with hysteresis smoothing. | Strict `bool` (`true` or `false`). |
| `presence` | Boolean | No | Sustained presence indicator held by configurable timeout. | Strict `bool` (`true` or `false`). |
| `separation_margin` | Float | **Yes** | Distance gap between the best and second-best matching zone signatures ($d_2 - d_1$), using whichever metric the active `ZoneClassifierConfig` specifies (default: `cosine_distance`, per Section 8; or `normalized_euclidean` when configured). Cross-reference Section 8 documentation in [`classification-strategy.md`](file:///c:/Users/mohan/Desktop/Academic/Software/WiSense%20Twin/backend_pipeline/docs/signal-processing/classification-strategy.md#4-separation-margin-vs-fabricated-confidence). | Must be `null` when `is_motion` is `false` or single-zone; finite $\ge 0.0$ if present. |
| `calibration_version`| Integer | **Yes** | Version integer of the `CalibrationProfile` used for classification. | Must be $\ge 1$ if present; `null` if uncalibrated. |

---

## 3. Deviations from the Original SRS Proposal

The original SRS draft outlined a rudimentary JSON message format. During the architecture audit, several critical flaws were identified and corrected in this implementation:

| Feature / Field | Original SRS Draft | Final Audited Implementation | Engineering Rationale |
| :--- | :--- | :--- | :--- |
| **Occupancy vs Motion** | `"status": "occupied"` | `"is_motion": bool`, `"presence": bool` | **RF Honesty**: Stationary individuals barely perturb CSI variance. Conflating motion detection with confirmed occupancy overstates physical capability. Separating instantaneous motion from timeout-derived presence provides honest, actionable telemetry. |
| **Confidence Metric** | `"confidence": 0.85` | `"separation_margin": float` | **Scientific Precision**: Distance to reference signatures under the configured metric (`cosine_distance` default, or `normalized_euclidean`) does NOT produce calibrated probabilities. Calling $d_2 - d_1$ "confidence" gives false assurances to downstream systems (UE5). It is strictly a diagnostic distance margin (see Section 8 in [`classification-strategy.md`](file:///c:/Users/mohan/Desktop/Academic/Software/WiSense%20Twin/backend_pipeline/docs/signal-processing/classification-strategy.md#4-separation-margin-vs-fabricated-confidence)). |
| **Protocol Evolution** | *Omitted* | `"schema_version": 1` | **Wire Resilience**: External consumers (UE5 C++ parsers) must reject incompatible payloads rather than silently misparsing fields or crashing on unexpected changes. |
| **Calibration Lineage** | *Omitted* | `"calibration_version": int` | **Provenance**: Enables auditability and logging so consumers know which calibration reference generated each zone assignment. |
| **Stream Scoping** | `"system_health": "OK"` included in state stream | *Excluded* | **Single Responsibility Principle**: Spatial state decisions are high frequency ($\approx 20\text{--}100\,\text{Hz}$). Health monitoring is diagnostic and low frequency, handled independently in Section 11. |
| **Motion Gating** | *Permitted zone guesses without motion* | `is_motion=False => zone_id=null, separation_margin=null` | **Zero Hallucination**: Classifying spatial zones from baseline thermal noise fabricates non-existent information. When motion is inactive, zone and margin are strictly `null`. |

---

## 4. Example Payloads

### 4.1. Active Motion with Confident Zone Classification
```json
{
  "schema_version": 1,
  "timestamp": 1700000005.123456,
  "zone_id": 2,
  "is_motion": true,
  "presence": true,
  "separation_margin": 0.034567,
  "calibration_version": 1
}
```

### 4.2. Active Motion with Ambiguous Classification (Separation Margin Too Low)
```json
{
  "schema_version": 1,
  "timestamp": 1700000006.234567,
  "zone_id": null,
  "is_motion": true,
  "presence": true,
  "separation_margin": null,
  "calibration_version": 1
}
```

### 4.3. Quiet State During Presence Hold (Motion Ceased, Timeout Sustaining Presence)
```json
{
  "schema_version": 1,
  "timestamp": 1700000008.345678,
  "zone_id": null,
  "is_motion": false,
  "presence": true,
  "separation_margin": null,
  "calibration_version": 1
}
```

### 4.4. Completely Quiet State (Idle Baseline)
```json
{
  "schema_version": 1,
  "timestamp": 1700000012.456789,
  "zone_id": null,
  "is_motion": false,
  "presence": false,
  "separation_margin": null,
  "calibration_version": 1
}
```

---

## 5. Server Architecture & Fault Tolerance

`WebSocketStatePublisher` manages WebSocket distribution with the following resilience characteristics:

1. **Synchronous Server Thread**:
   Uses `websockets.sync.server` running in a dedicated background daemon thread (`serve_forever()`), with controlled `shutdown()` for clean socket tear-down during shutdown or test teardown.

2. **Zero-Client Safe No-Op**:
   `publish(message)` checks active client count under a lock. If no clients are connected, the method exits immediately, eliminating JSON serialization and socket overhead.

3. **Multi-Client Broadcast**:
   Uses `websockets.sync.server.broadcast()` across all connected sockets. Write errors or client disconnects on one socket are isolated and skipped, preventing disruption to surviving clients.

4. **Dynamic Client Lifecycle**:
   Clients connect and disconnect asynchronously. Connection handlers capture remote peer addresses safely and manage membership in a thread-safe set.

---

## 6. Scope Boundaries & Roadmap Status

Per the project roadmap discipline:
- **Standalone Verification**: Section 10 builds and validates `StateMessage`, `WebSocketPublisher`, and `WebSocketStatePublisher` in complete isolation and against full 8-stage pipeline tests.
- **Application Integration**: Live wiring into `Application.run()` and `main.py` is explicitly deferred to the upcoming end-to-end integration section.
