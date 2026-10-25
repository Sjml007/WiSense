# Unreal Engine 5 — StateMessage v1 Consumption & Contract Reconciliation

## 1. Overview & Architectural Scope

In Section 17, the Unreal Engine 5.6 client (`WiSense` module) was updated to consume the authoritative **`StateMessage` v1** WebSocket stream defined in [`backend_pipeline/docs/architecture/communication-contracts.md`](file:///c:/Users/mohan/Desktop/Academic/Software/WiSense%20Twin/backend_pipeline/docs/architecture/communication-contracts.md).

This supersedes the prototype's legacy protocol (`{"zone": 2, "occupied": true}`):
* All occupancy semantics (`"occupied"`) have been removed from the C++ codebase.
* State is modeled honestly around distinct instantaneous **motion**, timeout-sustained **presence**, and motion-gated **zone identification**.
* Zone components are modified only upon state transitions, preventing redundant rendering updates.
* When disconnected or during silence, the digital twin resets to an honest idle state without showing stale spatial data.

---

## 2. Display State Derivation Rules

The pure function `ComputeDisplayState(const FWiSenseState& State)` maps wire state into visual digital twin directives:

| Operational State | Input Wire Fields (`FWiSenseState`) | `ActiveZone` | `bRoomMotion` | `bRoomPresence` | `bMotionWithUnknownZone` | Visual Effect in 3D Viewport |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **Active Motion in Zone $k$** | `is_motion=true`, `zone_id=k`, `presence=true` | `k` | `true` | `true` | `false` | Zone $k$ cube visible; other zones hidden |
| **Ambiguous Motion** | `is_motion=true`, `zone_id=null`, `presence=true` | `unset` | `true` | `true` | `true` | All zone cubes hidden (stored and logged only; no on-screen indicator until Section 18) |
| **Presence Hold Period** | `is_motion=false`, `zone_id=null`, `presence=true` | `unset` | `false` | `true` | `false` | All zone cubes hidden (stored and logged only; no on-screen indicator until Section 18) |
| **Quiet Idle Baseline** | `is_motion=false`, `zone_id=null`, `presence=false`| `unset` | `false` | `false` | `false` | All zone cubes hidden |

### Invariant Rules:
1. `ActiveZone` is set **only** when `is_motion == true` and `zone_id.IsSet()`.
2. A null `zone_id` is an expected, normal occurrence (ambiguous motion or quiet states); it **must never log an error**.
3. Initial state fix: Upon spawning in `SpawnZones()`, all zone components start **hidden** (`SetVisibility(false, true)`), resolving the prototype initial-state visibility bug.

---

## 3. Parsing Rules & Validation Specification

`FWiSenseState::ParseStateMessage(JsonString, OutState, OutError)` validates wire payloads with strict defense-in-depth:

1. **Payload Structure:** Must deserialize to a valid JSON object.
2. **`schema_version`:** Required numeric field. Must strictly equal `1`. Payloads with missing or unsupported schema versions are rejected.
3. **`timestamp`:** Required numeric field. Must be finite and positive (`> 0.0`).
4. **`is_motion`:** Required strict JSON boolean (`true` or `false`).
5. **`presence`:** Required strict JSON boolean (`true` or `false`).
6. **`zone_id`:** Nullable. If present:
   * Must be a JSON number (strings such as `"2"` are rejected).
   * Must be an integer (fractional numbers such as `2.5` are rejected).
   * Must be $\ge 1$ (zero and negative numbers are rejected; zones are 1-indexed).
7. **`separation_margin`:** Nullable. If present, must be finite and $\ge 0.0$.
8. **`calibration_version`:** Nullable. If present, must be integer $\ge 1$.
9. **Motion Gating Invariant:** If `is_motion == false`, then `zone_id` and `separation_margin` must both be `null` (or absent). Violations are rejected.

---

## 4. Python vs. Unreal Parser Asymmetries

The shared contract test suite (`contracts/fixtures/state_message/manifest.json`) revealed deliberate parser asymmetries between the Python backend dataclass and the strict Unreal C++ parser:

| Fixture Case | Raw Payload Snippet | Python `expected_python` | Unreal `expected_unreal` | Architectural Asymmetry Reason |
| :--- | :--- | :--- | :--- | :--- |
| `missing_schema_version` | `{"timestamp": ..., "zone_id": 2}` | **Pass (`true`)** | **Fail (`false`)** | Python's `StateMessage.from_dict` defaults `schema_version` to 1 if absent; Unreal client strictly mandates explicit schema versioning on the wire to prevent silent misinterpretation. |
| `unsupported_schema_version` | `{"schema_version": 2, ...}` | **Pass (`true`)** | **Fail (`false`)** | Python allows any `schema_version >= 1` (forward compatibility); Unreal v1 client strictly limits ingestion to `schema_version == 1` until v2 handlers are implemented. |
| `zone_as_string` | `{"zone_id": "2", ...}` | **Pass (`true`)** | **Fail (`false`)** | Python's `int(data["zone_id"])` coerces string digits into integers; Unreal C++ strictly checks `EJson::Number` and rejects string-typed fields. |
| `zone_fractional` | `{"zone_id": 2.5, ...}` | **Pass (`true`)** | **Fail (`false`)** | Python's `int(2.5)` silently truncates floating-point numbers; Unreal C++ strictly rejects fractional zone indices (`val == Floor(val)`). |
| `zone_zero` | `{"zone_id": 0, ...}` | **Fail (`false`)** | **Fail (`false`)** | Both environments reject non-positive zone IDs ($< 1$). |
| `motion_false_with_zone` | `{"is_motion": false, "zone_id": 2}` | **Fail (`false`)** | **Fail (`false`)** | Both environments enforce the motion-gating invariant (no zone without motion). |
| `legacy_occupied_format` | `{"zone": 2, "occupied": true}` | **Fail (`false`)** | **Fail (`false`)** | Both reject payloads missing timestamps and modern flags. |

---

## 5. How to Run Automation Tests

Automation tests are implemented in [`unreal_digital_twin/Source/WiSense/Tests/WiSenseStateMessageTest.cpp`](file:///c:/Users/mohan/Desktop/Academic/Software/WiSense%20Twin/unreal_digital_twin/Source/WiSense/Tests/WiSenseStateMessageTest.cpp) under the `WiSense.Contract.StateMessageVerification` test path.

### 5.1 In Unreal Editor GUI (Verify on UE 5.6):
1. Open `unreal_digital_twin/WiSense.uproject` in Unreal Editor 5.6.
2. Open **Tools** -> **Test Automation** (or **Window** -> **Developer Tools** -> **Session Frontend** -> **Automation**).
3. In the test hierarchy, expand **WiSense** -> **Contract**.
4. Check `StateMessageVerification`.
5. Click **Start Tests**. All display rules and all 12 manifest fixtures must report green checkmarks.

### 5.2 Via Command Line (Verify on UE 5.6):
```cmd
"<UnrealEngine_5.6_Root>\Engine\Binaries\Win64\UnrealEditor-Cmd.exe" ^
    "<Repo_Root>\unreal_digital_twin\WiSense.uproject" ^
    -ExecCmds="Automation RunTests WiSense.Contract.StateMessageVerification; Quit" ^
    -stdout -nullrhi
```

---

## 6. What Section 18 Changes

Section 17 provides protocol correctness, initial state hiding, and honest disconnection reset with hardcoded placeholder coordinates. Section 18 will address:
1. **Config-Driven Architecture:** Eliminating hardcoded `ws://127.0.0.1:8765` and hardcoded 4-zone locations by reading dynamic configuration.
2. **Dynamic Zone Counts:** Supporting arbitrary $N$-zone floor layouts discovered at runtime.
3. **Resilient Reconnection:** Automatic backoff and reconnection when the WebSocket publisher restarts.

---

## 7. Manual Verification Checklist (Unreal Machine)

Execute the following checklist on the machine with Unreal Engine 5.6 installed:

- [ ] **1. Compile Solution:**
  - Build `WiSense` in Visual Studio 2022 (`Development Editor | Win64`).
  - Confirm 0 compilation errors and 0 linker errors.
- [ ] **2. Run Automation Tests:**
  - Execute `WiSense.Contract.StateMessageVerification` via Session Frontend or CLI.
  - Confirm all 12 contract fixture assertions pass.
  - Confirm all 4 `ComputeDisplayState` rule tests pass.
- [ ] **3. Initial State Verification:**
  - Launch `WiSense.uproject` and press **Play in Editor (PIE)** without the backend running.
  - **Verify:** All 4 zone cubes are **hidden** upon spawning (resolves prototype bug).
  - **Verify:** Output Log reports: `WiSense: Spawned Zone 1 (initially hidden)`, ..., `WiSense: Reset to idle`.
- [ ] **4. Live Stream Visualization:**
  - In a terminal, run the live scenario generator:
    ```bash
    python scripts/dashboard_demo.py
    ```
  - In Unreal Editor, press **Play in Editor (PIE)**.
  - **Verify:** As the simulation moves across zones, the corresponding zone cube becomes visible and other cubes remain hidden.
  - **Verify:** During quiet/pause intervals, all cubes hide.
- [ ] **5. Disconnection & Honest Reset:**
  - Terminate `dashboard_demo.py` (`Ctrl+C`).
  - **Verify:** In Unreal Editor, the active zone immediately hides and the client transitions to idle. No stale zone remains visible.
- [ ] **6. Traffic & Log Volume:**
  - **Verify:** Per-message logging is restricted to `VeryVerbose`.
  - **Verify:** Output Log displays state transitions at `Display` without log flooding.
  - **Verify:** Null-zone messages (`zone_id: null`) do **not** trigger log errors.
