# WiSense-Twin — Project Overview (read this before implementing any section)

## What this project is
WiSense-Twin is a device-free human sensing system: an ESP32 transmitter generates WiFi
traffic, an ESP32 receiver captures Channel State Information (CSI), a Python backend
processes it into motion/zone state, and an Unreal Engine 5 digital twin visualizes that
state live in a 3D room model. It runs fully local — no cloud, no internet dependency.

This is a student Software Engineering course project, built incrementally, one section
at a time, by an AI implementation agent (you). Sections are assigned one prompt at a
time. **Only implement the section you are explicitly given. Do not implement ahead.**

## Section roadmap
Update the status column as sections complete. Do not skip ahead based on this list —
it's for context, not a queue to work through unprompted.

| # | Section | Status |
|---|---------|--------|
| 1 | Synthetic CSI Data Generator | done |
| 2 | Backend skeleton | done |
| 3 | Configuration & logging | done |
| 4 | Preprocessing pipeline (filtering, smoothing) | done |
| 5 | Feature extraction | done |
| 6 | Motion detection | done |
| 7 | Zone calibration | done |
| 8 | Zone classification | done |
| 9 | SQLite logging | done |
| 10 | WebSocket server | done |
| 11 | Health monitoring | done |
| 12 | Developer dashboard | done |
| 13 | ESP32 transmitter firmware | done |
| 14 | ESP32 CSI receiver firmware | done |
| 15 | Real hardware integration (swap synthetic → real source) | done |
| 16 | Adopt prototype | pending verification |
| 17 | Contract reconciliation | code complete, awaiting compile and test confirmation |
| 17B | Legacy Unreal zone-protocol output adapter (backend only) | done |
| 18 | Data-driven zones, connection handling, and stale watchdog | not started |
| 19 | Verify against the live stream and measure latency | not started |
| 20 | Zone indicator | not started |
| 21 | End-to-end integration | backend wiring done, hardware run pending |
| 22 | Performance testing | not started |
| 23 | Experimental validation (accuracy, confusion matrix) | not started |
| 24 | Final documentation | not started |
| 25 | Final demonstration prep | not started |

(List will be refined/reordered as sections are assigned — this is the current best
sequence, not a fixed contract.)

## Constraints every section must respect
These come from an architecture/feasibility audit done before implementation started.
Violating any of these silently is worse than asking — flag it instead.

- **Zone count is a config value, never hardcoded.** Four zones is a *target*, not a
  guarantee — the classifier and calibration store must work generically for N zones.
- **"Occupied" means motion-derived presence with a timeout**, not literal occupancy.
  Don't build or name anything as if a stationary person is reliably detected.
- **Amplitude is the primary CSI feature.** Phase-derived features are experimental —
  keep them optional/disableable, never a hard dependency for core functionality.
- **No fabricated confidence values.** Any `confidence` field must have a defined,
  documented derivation — never an arbitrary number.
- **Serial protocol (once real hardware is involved) must be binary/framed, not
  text/CSV.** This was a measured throughput risk, not a style preference.
- **Window size, sample rate, subcarrier count are all config, never constants.**
  Real values come from hardware spikes and may change.
- **Every algorithmic component (filter, classifier, confidence function) sits behind
  a swappable interface.** Don't couple modules directly to one implementation.
- **Zero ground-truth leakage into CSIRecord.** Downstream code consumes records exclusively
  through `CSIRecordSource`; records contain no synthetic tags or zone labels to ensure full parity with real hardware.
- **Timestamp is strictly UTC Unix epoch seconds (time.time()).** Relative or ad-hoc clocks
  are not permitted. Real serial readers timestamp frames on arrival with `time.time()`, and
  synthetic records must match that exact convention.
- **No hardcoded hardware COM ports or baud rates.** Serial connection parameters must remain
  null/unconfigured placeholders until explicitly provided by user config; defaults must never assume specific hardware ports.
- **Filter parameters are uncalibrated starting points.** Filter windows and thresholds must be
  empirically tuned against real hardware CSI once Section 15 is active, rather than assumed optimal.
- **Feature extraction window size sets physical detection latency floor.** The sliding window
  introduces a mandatory initial buffering delay of $W / f_s$ seconds (e.g. 1.0 s at 20 Hz with
  $W=20$); downstream components cannot respond faster than this window duration.
- **Motion thresholds must enforce dual-threshold hysteresis.** A single threshold causes boundary
  flicker; enter_threshold must strictly exceed exit_threshold to define a stable deadband.
- **Ground-truth isolation is mandatory during calibration.** Calibration is an offline batch
  process; FeatureVector and online pipeline stages remain strictly blind to ground truth. Labeling
  is encapsulated exclusively in calibration-specific data structures (LabeledFeatureSample).
- **Calibration profiles must be versioned, persistent, and zone-count-agnostic.** Profiles cannot
  be in-memory-only or hardcoded to 4 zones; they must be serializable to versioned JSON with provenance attribution.
- **Zone separability must be measured, not assumed.** Mutual interference and spectral overlap
  between zones must be evaluated via pairwise distance metrics before runtime classification is attempted;
  poor separability flags severe physical ambiguity.
- **Synthetic separability results are mechanisms tests, not hardware proof.** Synthetic Gaussian spatial
  perturbations prove mathematical and pipeline correctness, not that physical transceivers will discriminate
  physical zones without empirical multipath calibration.
- **Zone classification is strictly motion-gated.** Classifying spatial zones when `is_motion=False` is
  prohibited to prevent fabricating spatial presence from background noise; the classifier must short-circuit
  to `zone_id=None` and `separation_margin=None`.
- **Separation margin represents distance gap between nearest competitors, never confidence.** The quality
  metric is the difference between best-matching and runner-up reference signatures ($d_2 - d_1$); the term
  "confidence" is prohibited to avoid misleading downstream digital twin renderers and operators.
- **Physical window delay and computational pipeline latency must never be conflated.** Telemetry records
  store `window_latency_ms` ($W/f_s$) and `pipeline_latency_ms` as two distinct columns; non-realtime batch
  runs must record `pipeline_latency_ms=None` (Audit Item #14).
- **Database schema must be versioned and migrated via schema_migrations.** Schema creation and alterations
  must execute idempotently through the migration runner (Coding Standard #28).
- **Ground-truth columns in telemetry are strictly nullable validation fields.** `ground_truth_zone` and
  `ground_truth_motion` must remain null during online operation and are populated exclusively by external
  evaluation harnesses (Section 23).
- **Telemetry persistence operates exclusively via parameterized SQL.** Parameter concatenation or string
  formatting is strictly prohibited.
- **WebSocket wire contracts must be explicitly schema-versioned.** Protocol payloads must begin with
  `schema_version >= 1` so downstream consumers (UE5 C++ client) can detect breaking changes instead of silently misparsing.
- **Wire messages must separate instantaneous motion from sustained presence.** The ambiguous `"status": "occupied"`
  field from early SRS drafts is replaced by distinct `is_motion` and `presence` boolean flags.
- **WebSocket publishing must never block or fail on client disconnections.** Sockets must be managed thread-safely,
  write failures to individual clients must be isolated without aborting broadcasts to surviving peers, and `publish()`
  must be a safe zero-client no-op.
- **Health monitoring telemetry is decoupled from spatial state publishing.** Spatial state messages are strictly
  scoped to spatial decisions; health telemetry (`system_health`) is excluded from the high-frequency state stream and deferred to Section 11.
- **Health monitoring strictly observes and reports, never attempting automated recovery.** Reconnection and fault
  recovery are the responsibility of individual subsystem drivers (e.g. hardware UART in Section 15); health probes
  must remain read-only, non-blocking, and exception-isolated.
- **Health status vocabulary is strictly categorical with zero fabricated numeric scores.** Statuses use discrete
  states (`active`, `stale`, `disconnected`, `connected`, `unknown`, `healthy`, `degraded`, `critical`); numeric health
  percentages or arbitrary scores are prohibited.
- **CSI packet staleness is classified against dynamic, config-driven thresholds.** Staleness boundaries
  (`stale_after_seconds`, `disconnected_after_seconds`) are never hardcoded constants.
- **Dashboard must never display stale last-known values as active data upon disconnect.** Disconnection must
  immediately reset telemetry fields to honest empty states ("—") and display an explicit disconnected notification
  to prevent misleading operators.
- **Developer dashboard dynamically discovers and renders zones without assuming a fixed zone count.** The wire contract
  (`StateMessage`) does not convey `zone_count`; client UI visualizations must dynamically adapt up to the highest
  `zone_id` observed rather than hardcoding a 4-zone layout ($N$-zone compliance).
- **Health status display remains decoupled and deferred to Section 21.** `SystemHealth` telemetry is decoupled from
  the high-frequency 20 Hz `StateMessage` stream; the developer dashboard displays exclusively the Section 10 spatial
  wire stream until Section 21 application integration.
- **Transmitter parameters are strictly Kconfig-driven, never hardcoded.** Transmission packet rate and WiFi channel
  are configurable via `idf.py menuconfig` (`Kconfig.projbuild`) to allow empirical calibration without modifying C source.
- **Transmitter MAC address is fixed to 1a:00:00:00:00:00.** Hardware pairing between transmitter and receiver is anchored
  on this defined MAC filter, preventing ambient WiFi traffic from contaminating CSI channel estimates.
- **Transmitter enforces deterministic timing with zero cumulative drift.** The FreeRTOS transmission task utilizes hybrid
  pacing (`vTaskDelay` + hardware timer spin) at 1000 Hz scheduler resolution, guaranteeing zero cumulative drift and
  preventing transmission stalls.
- **WiFi Station power-save and packet aggregation are disabled.** `WIFI_PS_NONE` and `CONFIG_ESP_WIFI_AMPDU_TX_ENABLED=n`
  are required so every emitted frame produces an independent physical preamble for discrete receiver CSI estimation.
- **Receiver parameters are strictly Kconfig-driven, never hardcoded.** Baud rate (921600), WiFi channel, transmitter MAC filter,
  and FreeRTOS queue depth are configurable via `idf.py menuconfig` (`Kconfig.projbuild`).
- **WiFi CSI callback execution is strictly non-blocking (<2 us).** Performing synchronous I/O, UART transmission, or `printf()`
  inside `wifi_csi_rx_cb` is prohibited; the callback performs only MAC filtering and non-blocking FreeRTOS queue pushes, offloading
  serialization to an isolated Core-1 background task.
- **Serial CSI protocol is compact framed binary with magic sync and CRC-16.** Frames must prepend a 4-byte sync marker
  (`0xAA, 0x55, 0x57, 0x53`), a 28-byte fixed header, raw CSI byte array, and a 4-byte CRC-16-CCITT + trailer (`0x55, 0xAA`)
  to ensure robust resynchronization and prevent the throughput saturation observed with verbose ASCII CSV.
- **Transmitter MAC filtering is enforced in hardware/driver callback.** Receiver strictly discards any CSI frame not originating
  from `1a:00:00:00:00:00`, isolating ambient WiFi interference.
- **`dropped` in receiver telemetry indicates FreeRTOS queue overflows, not zero channel loss.** `dropped=0` proves that
  software queues and Core-1 UART tasks never saturated; true over-the-air packet loss must be calculated via sequence number
  continuity against the transmitter's emission rate.
- **`SerialCSISource` timestamps must strictly use host epoch seconds (`time.time()`).** Device-side boot clocks
  (`rx_timestamp_us`, `sender_ts_ms`) are relative and must never populate `CSIRecord.timestamp`, preserving Section 1 UTC epoch contract.
- **Subcarrier count is derived dynamically from frame payload length and enforced session-constant.** `subcarrier_count = payload_len // 2`
  adapts to HT20/HT40 frames, but mid-stream variance within an open session raises `ValueError` to prevent corrupted records.
- **Corrupted or torn frames trigger streaming resynchronization without losing stream state.** Frames failing CRC-16,
  bad trailers, or torn byte sequences (e.g., `walking_01.txt`) are skipped and increment `corrupt_frame_count`, seeking the next magic sync marker.
- **Serial fault recovery and automatic reconnection are strictly deferred.** Consistent with the project's deferred-scope pattern,
  `SerialCSISource` implements framing resilience without automated port reconnection logic.
- **Section 17 Unreal client consumes StateMessage v1 only, with no occupied semantics.** Downstream UE5 digital twin client models built under Section 17 strictly enforce authoritative `StateMessage` v1 schemas (`is_motion`, `presence`, `zone_id`, `separation_margin`), rejecting deprecated `occupied` boolean or legacy 2-field payloads. In contrast, the historical prototype Unreal build consumes the optional legacy zone protocol adapter (Section 17B).
- **Unreal client visuals are C++-only and runtime-spawned.** Zone representations must be spawned programmatically at runtime without relying on pre-placed Editor level assets.
- **Unreal client engine version is strictly 5.6.** Target rules and include orders (`EngineIncludeOrderVersion.Unreal5_6`) are locked to Unreal Engine 5.6 (BuildSettingsVersion.V5).
- **Unreal client wire parser enforces strict type validation without permissive coercion.** Unlike Python's dataclass parser, the C++ client rejects strings for numeric fields, rejects fractional zone indices, mandates explicit schema_version == 1, and enforces motion-gating invariants on the wire.
- **Unreal digital twin client resets to honest idle upon connection close and error.** When the WebSocket connection closes or encounters an error, all active zone indicators extinguish immediately without retaining stale spatial visuals.
- **Config-driven stale-stream watchdog (Section 18).** If no valid message is received for N seconds, the client resets to idle and shows a stale state, implemented without per-frame Tick.
- **Initial zone visuals must spawn hidden by default.** To prevent misleading operators before receiving the first live state decision, all runtime-spawned zone visuals initialize in a hidden state.
- **StateMessage v1 remains the authoritative contract.** The legacy zone protocol (`{"zone": k, "occupied": bool}`) is an optional output adapter, never a replacement.
- **`occupied` in legacy zone protocol denotes presence-held last active zone only.** It must never be described or marketed as literal human occupancy detection.
- **Legacy zone protocol enforces single-person mutual exclusivity.** At most one zone can be occupied at any time; zone transitions emit the previous zone's `occupied=false` before the new zone's `occupied=true`.
- **Initial full snapshot is sent upon client connection.** Newly connected clients receive a complete snapshot across all zones (1..zone_count) to reliably initialize state (e.g., hiding cubes in the Unreal prototype).
- **Prototype Unreal build lacks stale-stream handling (Section 17B).** The prototype Unreal build has no stale-stream handling, and backend-side resets on stall are deferred to Section 21.
- **Fail-fast hardware validation (Section 21).** In hardware mode, `motion_config_path` must be explicitly set (never silently inherit synthetic thresholds). On the first hardware record, if a calibration profile is loaded, `profile.subcarrier_count` must strictly equal `record.subcarrier_count` (raising a descriptive error naming both counts and explaining that a profile must be calibrated on this hardware). In no-zone mode (`calibration_profile_path=None`), the legacy wire format is rejected at startup.
- **No silent use of synthetic-scale thresholds (Section 21).** Physical CSI variance scales differ by orders of magnitude from synthetic simulation; deployments must never silently run with default synthetic thresholds. Hardware startup rejects missing motion configuration.
- **Stall handling by wire format (Section 21).** When no record arrives for `disconnected_after_seconds`, `runner.reset()` is called so analysis windows never straddle the gap. In `legacy_zone` mode, the bridge emits all-false once to extinguish prototype cube visuals. In `state_message_v1` mode, the backend publishes nothing because silence is the authoritative watchdog signal for the Section 17/18 UE5 digital twin client.

## Interfaces / contracts
`src/wisense_backend/contracts/` is the source of truth for shared interfaces (e.g.
`CSIRecordSource`). Read what's already defined there before adding a new one —
extend or implement existing interfaces rather than creating parallel ones.

## Workflow for every section
1. Read this file and the specific section prompt you were given.
2. Inspect the existing repo — don't assume what exists.
3. If a structural or interface decision isn't specified, propose it and explain why
   before writing code — don't decide silently on something durable.
4. Implement only the assigned section's scope.
5. Run tests, update relevant docs.
6. Update this file: mark your section's status as done, and update the constraints
   list if this section surfaced a new one worth remembering.
7. Report changed files and stop.
