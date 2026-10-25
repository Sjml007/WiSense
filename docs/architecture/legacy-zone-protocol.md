# Legacy Unreal Zone Protocol & Output Adapter (Section 17B)

## 1. Overview & Architectural Role

WiSense-Twin broadcasts real-time RF spatial decisions over WebSocket. The authoritative, canonical wire contract for the project is **`StateMessage` v1** (defined in [`communication-contracts.md`](file:///c:/Users/mohan/Desktop/Academic/Software/WiSense%20Twin/backend_pipeline/docs/architecture/communication-contracts.md)).

Section 17B introduces an **optional output adapter and bridge layer** (`LegacyZoneAdapter` and `LegacyZoneBridge`) designed to emit incremental occupancy updates in the historical 2-field JSON wire format consumed by the original Unreal Engine 5 prototype handoff ([`prototype-handoff.md`](file:///c:/Users/mohan/Desktop/Academic/Software/WiSense%20Twin/docs/unreal/prototype-handoff.md)).

> [!IMPORTANT]
> **Adapter, Not a Replacement**: `StateMessage` v1 remains the authoritative contract across the entire WiSense-Twin ecosystem. This adapter does not replace or modify the authoritative schema. It acts as an optional downstream translator for backwards compatibility with the pre-existing Unreal prototype build. The default output format for the publisher and dashboard remains `state_message_v1`.

---

## 2. Exact Wire Specification

The legacy wire protocol represents incremental updates to individual spatial zones.

### 2.1. Payload Format
Every emitted frame is a compact JSON string with **strictly two keys** and **strict native JSON types**:

```json
{"zone":2,"occupied":true}
```

Emissions are produced via:
```python
json.dumps({"zone": zone, "occupied": occupied}, separators=(",", ":"))
```

### 2.2. Field Definitions

| Field | JSON Type | Nullable | Description & Constraints |
| :--- | :--- | :--- | :--- |
| `zone` | `integer` | No | 1-indexed spatial zone identifier ($1 \le \text{zone} \le \text{zone\_count}$). Never a string or float. |
| `occupied` | `boolean` | No | `true` if presence is active in this zone; `false` if cleared. Never an integer (`1`/`0`) or string. |

No extra keys, timestamps, margins, or wrapper metadata are permitted on the wire.

---

## 3. Semantics & Behavioral Invariants

> [!CAUTION]
> ### Prominent Semantic Notice: "Occupied" is a Legacy Wire Name
> The field name `occupied` is a legacy wire token inherited from the original Unreal prototype. It denotes **"motion-derived presence held in this zone"**, **NOT** verified human occupancy detection.
> 
> Due to the physical characteristics of Channel State Information (CSI), a stationary individual produces baseline variance indistinguishable from an empty room. When motion ceases, presence is sustained solely by a temporal decay hold; once the timeout expires, the zone decays to `occupied=false`. This protocol must **never** be represented as occupancy detection.

### 3.1. Single-Person Scope
The adapter operates under a single-person tracking scope: **at most one zone is occupied (`occupied=true`) at any given moment**.

### 3.2. State Transition Rules

The adapter translates incoming authoritative `StateMessage` decisions according to the following truth table:

| Incoming `StateMessage` | Adapter State & Output Behavior |
| :--- | :--- |
| `is_motion=True`, `zone_id=k` ($1 \le k \le \text{zone\_count}$) | If $k$ is not already the occupied zone:<br>1. Emit `{"zone": <previous_zone>, "occupied": false}` (if a zone was occupied).<br>2. Emit `{"zone": k, "occupied": true}`.<br>3. Set occupied zone to $k$. |
| `is_motion=True`, `zone_id=None` | Ambiguous classification (e.g. low separation margin). **Hold current zone**; emit nothing. |
| `is_motion=False`, `presence=True` | Motion ceased, presence hold decay active. **Hold current zone**; emit nothing. |
| `presence=False` | Presence expired (empty baseline). If a zone is currently occupied, emit `{"zone": <occupied_zone>, "occupied": false}` and clear state to idle. If already idle, emit nothing. |
| `zone_id` outside $1..\text{zone\_count}$ | Out-of-bounds anomaly. **Ignore decision**; log a throttled warning; emit nothing. |
| Repeat of current state | Redundant decision epoch. **Emit nothing** (suppress duplicate wire transmissions). |

### 3.3. Switch Ordering Invariant
When switching between two zones (e.g. Zone 1 $\to$ Zone 2), the adapter guarantees that the previous zone's clearance is emitted **before** the new zone's activation:
```text
1. {"zone":1,"occupied":false}
2. {"zone":2,"occupied":true}
```
This prevents digital twin visualizers from observing two concurrently occupied zones.

---

## 4. Why an Adapter and What Information is Dropped

The legacy format is deliberately minimal and drops critical telemetry present in `StateMessage` v1:

| Dropped Telemetry Field | Consequence of Omission |
| :--- | :--- |
| `is_motion` | Downstream client cannot differentiate instantaneous motion from a decaying presence hold. |
| `presence` | Downstream client cannot tell if an empty state was caused by intentional decay or motion gating. |
| `separation_margin` | Downstream client has no measure of spatial ambiguity or RF classification quality ($d_2 - d_1$). |
| `calibration_version` | Downstream client cannot verify whether spatial decisions originate from an active or stale calibration profile. |
| `timestamp` | Downstream client cannot measure transport latency, jitter, or temporal drift. |

Because of these omissions, the web developer dashboard (`dashboard/index.html`) **speaks `StateMessage` v1 only**. The legacy zone format is strictly reserved for legacy visualizers.

---

## 5. Architectural Components

### 5.1. `LegacyZoneAdapter` (`wisense_backend.communication.legacy_zone_adapter`)
A pure, synchronous state translator:
- Initialized with `zone_count` (never hardcoded to 4) and an optional monotonic clock.
- `process(state: StateMessage) -> list[LegacyZoneMessage]`: Translates a single state epoch.
- `snapshot() -> list[LegacyZoneMessage]`: Returns a full list of all zones $1..\text{zone\_count}$, with `occupied=False` for empty zones and `occupied=True` for the active zone.
- `reset() -> None`: Clears internal occupancy tracking.

### 5.2. `LegacyZoneBridge` (`wisense_backend.communication.legacy_zone_adapter`)
A thread-safe bridge binding an adapter to a `WebSocketStatePublisher`:
- **Atomic Connect & Registration Critical Section**: Initial `snapshot()` delivery to a newly connecting client and registration of that client into the publisher's broadcast set are executed as **one single critical section** under the bridge's lock (`threading.RLock`). The client is not in the broadcast set before receiving its snapshot.
- **Identical Lock Ordering**: Lock acquisition order is strictly identical across all execution paths (`bridge._lock` first, then `publisher._clients_lock` second). This eliminates race conditions where a concurrent transition could be broadcast before the snapshot, and guarantees absolute deadlock freedom.
- **On-Connect Hook & Error Isolation**: Registers an on-connect hook on the publisher so every new client immediately receives `snapshot()` before any broadcast frames. Hook failures are caught and isolated so a failing hook never drops the client socket.
  - *Rationale*: The prototype Unreal project initializes with all cubes visible; sending an immediate snapshot hides inactive cubes explicitly upon connection.
- **Periodic Resynchronization**: When `legacy_resync_interval_seconds > 0`, re-sends the full `snapshot()` whenever the configured interval has elapsed since the last transmission. Setting `resync_interval_seconds = 0` disables periodic resync. No background thread is spawned; timing is evaluated synchronously inside `publish_state()`.
- **Clean Shutdown & Bounded Flush**: `close()` performs a best-effort broadcast of `occupied=false` for all zones $1..\text{zone\_count}$ before stopping the publisher, ensuring visual cubes extinguish on server exit. The demo script executes a best-effort bounded flush (200 ms) so in-flight close frames drain across OS TCP buffers to clients before `publisher.stop()` terminates the listening socket.

---

## 6. Known Limitations

1. **No Incremental Snapshots During Stream Gaps**: Except for client initial connection and periodic resynchronization, no full-state bursts are emitted. If a client drops packets without reconnecting, it may retain desynchronized state until the next resync interval.
2. **Stream Stall Behavior**: The prototype Unreal build has no stale-stream handling. If the CSI stream stalls or the transmitter powers off abruptly without sending frames, legacy clients retain the last known zone state; backend-side resets on stall are deferred to Section 21.
3. **Single Person Tracking Only**: The 2-field legacy protocol assumes a single localized target. Multi-person presence cannot be represented without extending the schema.
4. **Section 17 Unreal Build Incompatibility**: The updated Unreal Engine 5 C++ client constructed in Section 17 strictly enforces `StateMessage` v1 parsing and **rejects** this 2-field JSON format by design. The format selected must match the specific Unreal build deployed on the host.
5. **Connect Hook Latency Under Lock**: The connect hook sends initial snapshot frames under the bridge lock (`bridge._lock`), so one blocked client socket can delay publishing.

---

## 7. Manual Verification Checklist for Unreal Machine

When testing against a machine running the prototype Unreal Engine 5 visualizer:

1. **Start the demo server with the legacy wire format**:
   ```powershell
   python scripts/dashboard_demo.py --wire-format legacy_zone
   ```
2. **Verify Startup Banner**:
   Confirm the terminal output displays:
   ```text
     Wire Format:        legacy_zone
     Note: Browser dashboard (/dashboard/index.html) speaks StateMessage v1 only.
   ```
3. **Launch the Unreal Prototype**:
   - Open the Unreal Engine prototype project.
   - Press **Play** in Editor.
   - Confirm Unreal console displays:
     ```text
     WiSense: Connecting to ws://127.0.0.1:8765
     WiSense: WebSocket connected.
     ```
4. **Confirm Initial Snapshot**:
   - Confirm that all four zone cubes immediately hide upon connection (initial `snapshot()` hiding cubes).
5. **Observe Sequential Zone Activity**:
   - As the synthetic scenario progresses, confirm Zone 1 cube appears, then hides.
   - Zone 2 cube appears, then hides.
   - Zone 3 cube appears, then hides.
   - Zone 4 cube appears, then hides.
   - Confirm that two cubes are never visible simultaneously.
6. **Confirm Shutdown Extinction**:
   - Press `Ctrl+C` in the Python terminal.
   - Confirm all cubes immediately hide on the digital twin as `bridge.close()` sends all-false before termination.
