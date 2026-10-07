# Health Monitoring Architecture & Subsystem Observability

## 1. Overview & Core Purpose

The `wisense_backend.monitoring` subsystem provides structured observability into the operational readiness of the WiSense-Twin backend pipeline. It continuously assesses whether data streams are active, persistent storage is functioning, and external visualizers (Unreal Engine 5 digital twins or developer dashboards) are connected.

Health snapshots are captured via an on-demand pull model: calling `get_current_health()` produces an immutable, validated [`SystemHealth`](file:///c:/Users/mohan/Desktop/Academic/Software/WiSense%20Twin/backend_pipeline/src/wisense_backend/contracts/health_status.py) dataclass.

---

## 2. Architectural Boundary: Observability vs. Fault Recovery

> [!IMPORTANT]
> **Observation-Only Scope (Audit Item #19)**:
> This component observes, measures, and reports the health of individual subsystems. It **does NOT attempt automated fault recovery or reconnection logic** (such as serial port reopening, SQLite database reconnection, or WebSocket server socket recreation).
>
> Automated fault recovery is the architectural responsibility of each subsystem's own hardening layer (e.g., Section 15 for hardware UART serial readers). Attempting ad-hoc reconnection inside an observability probe conflates monitoring with subsystem lifecycle management and causes silent reconnect loops.

---

## 3. Honest, Categorical Status Vocabulary

In strict accordance with the project's scientific discipline (rejecting fabricated metrics, as established with `separation_margin`), `SystemHealth` uses discrete, well-defined string categories. **No arbitrary numeric "health scores" or percentage ratings (e.g., "85% healthy") are generated.**

### 3.1 Subsystem Status Vocabulary

| Subsystem | Field | Allowed Values | Semantics |
| :--- | :--- | :--- | :--- |
| **CSI Ingestion** | `csi_source_status` | `"active"` | At least one `CSIRecord` received within the configured `stale_after_seconds`. |
| | | `"stale"` | No `CSIRecord` received within `stale_after_seconds`, but elapsed time $< \text{disconnected\_after\_seconds}$. |
| | | `"disconnected"` | No records ever received (`None`), or elapsed time $\ge \text{disconnected\_after\_seconds}$, or probe failed. |
| **Database** | `database_status` | `"connected"` | Database backend connection is verified and active. |
| | | `"disconnected"` | Database connection severed, lock failed, or probe raised an exception. |
| | | `"unknown"` | Database probe not yet registered or connection state unverified. |
| **WebSocket** | `websocket_client_count` | Non-negative integer | Count of currently connected WebSocket visualizers ($\ge 0$). Zero indicates normal idle operation. |

---

## 4. CSI Source Staleness Detection

Staleness detection resolves Audit Item #19: physical WiFi transmitters or ESP32 serial readers can stall or fail without explicitly closing the socket or serial port.

Given a probe returning the UTC timestamp of the most recent `CSIRecord` ($t_{\text{last}}$), the monitor computes:
$$\text{age} = \max(0.0, t_{\text{now}} - t_{\text{last}})$$

Stream status is evaluated against configuration thresholds:
$$\text{csi\_source\_status} = \begin{cases} \text{"disconnected"} & \text{if } t_{\text{last}} \text{ is None} \\ \text{"active"} & \text{if } \text{age} < \tau_{\text{stale}} \\ \text{"stale"} & \text{if } \tau_{\text{stale}} \le \text{age} < \tau_{\text{disconnected}} \\ \text{"disconnected"} & \text{if } \text{age} \ge \tau_{\text{disconnected}} \end{cases}$$

### Configuration Schema (`config/health_monitor_config.json`)

```json
{
  "stale_after_seconds": 2.0,
  "disconnected_after_seconds": 5.0
}
```

Constraints enforced:
$$0.0 < \tau_{\text{stale}} < \tau_{\text{disconnected}} < \infty$$

---

## 5. Deterministic `overall_status` Rollup Matrix

The composite system state `overall_status` is derived deterministically from the individual subsystem states using a priority hierarchy:

```
                  [ Subsystem Health Inputs ]
                               │
               Is CSI or Database DISCONNECTED?
                              / \
                        Yes  /   \ No
                            ▼     ▼
                      [ CRITICAL ] Is CSI STALE or DB UNKNOWN?
                                    / \
                              Yes  /   \ No
                                  ▼     ▼
                            [ DEGRADED ] [ HEALTHY ]
```

### Rollup Decision Table

| `csi_source_status` | `database_status` | `overall_status` | Rationale |
| :--- | :--- | :--- | :--- |
| `"active"` | `"connected"` | **`"healthy"`** | All critical ingest and persistence paths operational. |
| `"stale"` | `"connected"` | **`"degraded"`** | Ingest experiencing packet delays or high jitter. |
| `"active"` | `"unknown"` | **`"degraded"`** | Database persistence state is unverified. |
| `"stale"` | `"unknown"` | **`"degraded"`** | Both ingest and persistence are in non-fatal degraded states. |
| `"disconnected"` | *Any* | **`"critical"`** | RF sensing stream severed; pipeline has no data. |
| *Any* | `"disconnected"` | **`"critical"`** | Telemetry logging is completely broken. |

---

## 6. On-Demand Pull Model Architecture

Rather than running an asynchronous polling loop or dedicated background thread, `SystemHealthMonitor` uses an on-demand pull model:
- Probes are registered once during system initialization (e.g., in Section 21's integration).
- `get_current_health()` evaluates probes synchronously in $< 1\,\text{ms}$.
- Exception isolation: any probe raising an unhandled exception is caught, logged, and treated as `"disconnected"`, guaranteeing that monitoring checks never crash the caller.

---

## 7. Future Integration & Scope Boundaries

Per the roadmap discipline:
1. **Live Application Wiring Deferred**: Wiring `SystemHealthMonitor` into `Application.run()` and `main.py` is deferred to Section 21.
2. **WebSocket Health Publishing Deferred**: In Section 10, `system_health` was deliberately excluded from the spatial `StateMessage` stream. When health telemetry is broadcast over WebSockets, it will consume snapshots from this `HealthMonitor` across a dedicated channel or periodic diagnostic payload.
