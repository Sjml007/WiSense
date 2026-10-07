# WiSense-Twin — Project Handoff Document

This file complements `PROJECT_OVERVIEW.md` (which holds the architecture, roadmap,
and durable constraints). This file captures the *state of work and conversation
history* — what has actually happened so far, in what order, with what evidence —
so a new AI assistant can pick up the project without re-deriving context.

Everything below is drawn directly from the working conversation with the previous
assistant (Claude) and the code/test output the user pasted into it. Anything not
directly verifiable from that conversation is marked **[UNKNOWN]** rather than
inferred or invented.

---

## 1. Current Project Status

- **Sections 1–12 of the roadmap are implemented and approved.** All of them operate
  against `SyntheticCSISource` (Section 1) — no real ESP32 hardware has been
  integrated into the software pipeline yet.
- **Hardware track is separate and behind the software track.** ESP-IDF is installed
  and working on the user's Windows machine (v5.2.8). Two physical ESP32 boards are
  in hand and were given step-by-step instructions to run two feasibility spikes
  (raw CSI capture working; real serial throughput measurement). **Whether those
  spikes were actually completed and what they showed is [UNKNOWN]** — the user
  never reported back the results before starting this handoff.
- The working method throughout has been: the previous assistant (acting as
  architect/reviewer) writes a detailed implementation prompt for one roadmap
  section at a time; the user pastes that prompt into **Antigravity** (an
  implementation agent); Antigravity implements it and reports back (a "walkthrough"
  plus full source code); the assistant reviews the actual code (not just the
  walkthrough claims) against the prompt's Definition of Done, flags gaps or bugs,
  and either approves the section or sends a scoped correction prompt back through
  the user to Antigravity. This review-then-approve loop is the established
  workflow and should continue.
- Starting partway through the project (after a repeated miss), every implementation
  prompt now explicitly instructs Antigravity to paste full file contents into its
  own walkthrough report, rather than only describing changes — this was added as a
  standing instruction from Section 12 onward specifically to reduce review
  round-trips.

## 2. Exact Section/Task Being Worked On Now

- **Last fully approved section: Section 12 (Developer Dashboard).** Confirmed
  clean after a documentation-duplication correction (a duplicated
  `dashboard-usage.md` was collapsed to one canonical copy with a pointer file).
- **Next roadmap item per `PROJECT_OVERVIEW.md`: Section 13 (ESP32 transmitter
  firmware).** This is the first roadmap section that requires real hardware/
  firmware work rather than pure Python backend work.
- **Before Section 13 was assigned, the previous assistant's stated position was:**
  do not start Section 13 until the two hardware feasibility spikes (see §8 below)
  have concrete results, because Section 13's own implementation prompt should be
  informed by what those spikes reveal (e.g., confirmed working ESP-IDF/CSI-example
  combination, measured serial throughput, binary vs. text framing decision). No
  Section 13 prompt has been written yet.
- The handoff is happening at this exact point: Section 12 done, hardware spike
  results pending, Section 13 not yet started.

## 3. Completed Implementation Details (by section)

All of the following were implemented by Antigravity per assistant-written prompts,
and reviewed/approved by the assistant against each prompt's Definition of Done.

### Section 1 — Synthetic CSI Data Generator
- `contracts/csi_source.py`: `CSIRecord` (frozen dataclass: timestamp as **epoch
  seconds**, subcarrier_count, amplitudes, optional phases, optional rssi/channel/
  sequence_number) and `CSIRecordSource` (a `Protocol`) — the shared interface both
  synthetic and future real hardware sources implement.
- `ingestion/synthetic_source.py`: `SyntheticCSISource`, `ScenarioConfig`,
  `ScenarioEvent`. Fully config-driven (zone count, subcarrier count, sample rate,
  duration, events list with start/end time + zone_id + motion_type + intensity).
  Zone spatial sensitivity modeled via Gaussian masks over subcarrier index, centered
  per-zone (`center = ((z - 0.5)/N) * subcarrier_count`). No ground-truth (zone)
  leakage into `CSIRecord` — a separate `records_with_ground_truth()` method exists
  only for test/harness use.
- `config/synthetic_scenarios/example.json`: canonical test scenario — 4 zones, 64
  subcarriers, 20 Hz, 15s duration, single motion event in Zone 2 from t=5s to t=10s
  → exactly 300 records.
- One correction applied here: original `run_synthetic.py` doc/command mismatch was
  fixed; `requirements.txt` confirmed to include numpy/pytest; doc language softened
  from "realistic multipath" to "plausible, illustrative" model (this caveat —
  synthetic ≠ validated real-world signature — recurs throughout later sections'
  docs).

### Sections 2–3 — Backend Skeleton + Configuration & Logging
- `config/app_config.py`: `AppConfig` (frozen dataclass) — websocket host/port,
  database_path, log_level/format, csi_source_mode ("synthetic"/"hardware"),
  synthetic_scenario_path, serial_port/serial_baud_rate (both default `None` —
  no hardcoded COM ports anywhere).
- `logging_setup.py`: `configure_logging()` — idempotent handler reconfiguration,
  text and JSON formatters.
- `app.py`: `Application` class — loads config, sets up logging, instantiates the
  configured `CSIRecordSource` (`hardware` mode currently raises
  `NotImplementedError("real hardware source not yet implemented — see Section 14")`
  — this is intentional/expected, not a gap), `run()` loop counts records via the
  `CSIRecordSource` interface, `stop()` for graceful shutdown.
- `main.py`: CLI entry point, SIGINT/SIGTERM → `Application.stop()`.
- **Correction applied:** the original `test_stop_signal_shuts_down_cleanly` test
  used a non-realtime scenario, so calling `stop()` shortly after `run()` started
  almost certainly never actually interrupted anything mid-stream (the whole 300
  records had likely already processed near-instantly) — the assertion
  `record_count >= 0` was trivially true either way. This was flagged repeatedly
  across several subsequent sections before the user finally confirmed **"the stop
  signal is fixed."** The corrected version (per the correction prompt) should use a
  `realtime=True` scenario and assert `record_count < 300` strictly, verified to
  fail if `stop()` is commented out. **The assistant never received the corrected
  source code to independently re-verify this fix — it is confirmed only by the
  user's statement, not by code review.** Flag this as worth a spot-check.

### Section 4 — Preprocessing Pipeline
- `contracts/preprocessor.py`: `Preprocessor` Protocol (`process`, `reset`,
  `process_stream`).
- `preprocessing/hampel_filter.py`: `HampelFilter` — rolling median/MAD-based
  outlier suppression, config-driven `window_size`, `threshold_multiplier`,
  `min_mad` (floor to prevent false positives on flat signal), optional
  `filter_phases` (default off). Confirmed: filters compute median/MAD from *raw*
  input history, not fed-back filtered output (no feedback-loop bug).
- `preprocessing/moving_average.py`: `MovingAverageFilter` — causal rolling mean,
  config-driven `window_size`.
- `preprocessing/pipeline.py`: `PreprocessingPipeline` — chains an ordered,
  config-driven list of filters (itself conforms to `Preprocessor`, composite
  pattern); `enabled: false` in config genuinely skips construction of that filter
  step, not just a no-op flag.
- Observed effect (documented, not a bug): the moving-average filter, being a
  low-pass filter, partially suppresses the oscillatory motion signal itself, not
  just noise — raw injected-motion variance showed >20x increase pre-filter, only
  ~2x post-filter in later pipeline tests. Flagged as a real interaction to keep in
  mind for calibration tuning (Section 7), not a bug to fix in Section 4.

### Section 5 — Feature Extraction (windowed variance)
- `contracts/feature_extractor.py`: `FeatureVector` (window_start/end_timestamp,
  sample_count, subcarrier_count, variance_per_subcarrier, aggregate_variance —
  strictly zone-agnostic, no zone field) and `FeatureExtractor` Protocol.
- `features/sliding_window.py`: `SlidingWindowBuffer` — config-driven
  `window_size_samples`, `window_step_samples` (default 1 = continuously sliding),
  `emit_partial` (default `False` — no output until window fully fills; this
  buffering is the actual physical source of detection latency, documented
  explicitly as "Audit Item #14").
- `features/variance_extractor.py`: `VarianceFeatureExtractor` — unbiased sample
  variance per subcarrier + mean aggregate variance over the window; degenerate-ddof
  edge case (very few samples) guarded against.
- Full pipeline test (Sections 1→4→5): 300 raw records → 281 `FeatureVector`s
  (300 − window_size(20) + 1), matching a 1.0s window-fill latency at 20 Hz.
  Spatial discriminability confirmed: Zone 2 subcarrier band variance >1.8x the
  distant Zone 4 band during the scenario's motion interval.

### Section 6 — Motion Detection
- `contracts/motion_detector.py`: `MotionState` (timestamp, `is_motion`,
  `presence`, `variance_at_detection` — explicitly a diagnostic value, never a
  confidence score) and `MotionDetector` Protocol. **Strict naming discipline**: no
  field or component anywhere is named `occupancy`/`is_occupied` — this reflects
  the project's core audit finding that CSI variance detects *motion*, not
  confirmed stationary occupancy.
- `detection/threshold_motion_detector.py`: `ThresholdMotionDetector` — dual
  threshold hysteresis (`enter_threshold` > `exit_threshold`, config-driven, default
  0.10/0.05) to prevent flicker at a borderline value; `presence_timeout_seconds`
  (default 3.0) sustains `presence=True` for a fixed window after the last confirmed
  motion — explicitly a *timeout heuristic*, not real occupancy sensing.
- **Correction applied:** `ThresholdMotionConfig.from_dict()`'s fallback defaults
  (0.40/0.20) originally didn't match the dataclass field defaults (0.10/0.05) or
  the canonical sample config — fixed so both paths produce identical defaults, with
  a regression test added.
- Full pipeline test confirmed: pre-motion quiet -> `is_motion=False,
  presence=False`; active motion -> both `True`; post-motion -> `is_motion=False` but
  `presence=True` sustained by the timeout tail; well past the timeout ->
  `presence=False`.

### Section 7 — Zone Calibration
- `contracts/calibration.py`: `ZoneSignature` (per-zone mean per-subcarrier
  variance + mean aggregate variance + sample_count) and `CalibrationProfile`
  (zone_count, subcarrier_count, `calibration_version`, `created_at`, `source`
  = "synthetic"/"hardware", `zone_signatures` dict — generic for any N zones, never
  hardcoded to 4). JSON save/load round-trip verified with full precision.
- `calibration/labeled_sample.py`: `LabeledFeatureSample` (`FeatureVector` +
  optional `zone_id`, `None` = quiet baseline) — kept strictly separate from the
  online pipeline's `FeatureVector` to preserve ground-truth blindness.
- `calibration/calibrator.py`: `ZoneCalibrator` — one-shot batch process; requires
  every configured zone to have at least one labeled sample; computes per-zone mean
  signatures.
- `calibration/separability.py`: `compute_cosine_distance`,
  `compute_normalized_euclidean_distance` (`= sqrt(cosine_distance)` —
  **documented as NOT directly comparable across metrics**: a threshold tuned for
  one metric doesn't mean the same strictness under the other), `compute_separability`
  — evaluates all pairwise zone combinations against a configurable threshold,
  produces a `SeparabilityReport` (WELL/POORLY-SEPARATED per pair + overall verdict).
  This operationalizes the original architecture audit's most important open
  question (whether N zones are actually discriminable from a single TX/RX link) —
  turns it from an assumption into a measurement.
- **Two corrections applied:** (1) `CalibrationProfile.zone_signatures` was a
  mutable dict despite the dataclass being frozen — fixed via
  `types.MappingProxyType`, with a test proving mutation now raises `TypeError`.
  (2) An unused `baseline_samples` list in `ZoneCalibrator.calibrate()` (quiet
  samples were collected but never used) — resolved by removing the dead
  partitioning logic entirely rather than building out an unrequested baseline
  feature.
- Canonical 4-zone/64-subcarrier profile (`default_calibration.json`) confirmed
  fully separable (all 6 pairs WELL-SEPARATED, adjacent-zone pairs consistently
  scoring lower than non-adjacent pairs — physically sensible given the Gaussian
  zone-mask overlap pattern from Section 1).

### Section 8 — Zone Classification
- `contracts/zone_classifier.py`: `ZoneClassification` (timestamp, `zone_id:
  Optional[int]`, `is_motion`, `calibration_version`, `separation_margin:
  Optional[float]` — **never named or treated as "confidence"**) and
  `ZoneClassifier` Protocol. Motion-gating enforced as a hard dataclass invariant:
  `is_motion=False` forces `zone_id=None` and `separation_margin=None` (raises
  `ValueError` otherwise, not just documented).
- `classification/nearest_signature_classifier.py`: `NearestSignatureZoneClassifier`
  — reuses Section 7's distance metric functions directly (no duplicated math);
  ranks all zone signatures by distance, picks nearest, computes
  `separation_margin = second_best_dist - best_dist`; optional config-driven
  `min_separation_margin` — if the margin is too small even with motion present,
  reports `zone_id=None` (ambiguous match) rather than guessing. **Two independent
  paths to "unknown"**: no motion, or ambiguous margin.
- Audits the loaded `CalibrationProfile`'s separability at classifier construction
  time (via Section 7's `compute_separability`) and logs a warning (not an error) if
  any zone pairs are poorly separated — this is the direct runtime payoff of Section
  7's work.
- Full 8-stage integration test (Sections 1->4->5->6->7->8): 4-zone sequential motion
  scenario correctly classifies each active zone during its motion window and
  reports `zone_id=None` during quiet intervals.
- **Non-blocking note left open (not fixed):** `classify_stream()` zips `features`
  and `motion_states` positionally with no check that they actually correspond to
  the same window (relies on both streams being derived from the same pipeline run,
  which is true today but is an unguarded assumption for future refactors).

### Section 9 — SQLite Logging
- `contracts/database.py`: `EventLogRecord` — addresses every gap the *original
  architecture audit* found in the initial proposed DB schema: `run_id` (caller-
  supplied session grouping), full timestamp/window fields, decision fields
  (`is_motion`, `presence`, `zone_id`, `separation_margin`, `calibration_version`),
  **`window_latency_ms` and `pipeline_latency_ms` kept as two separate fields, never
  merged** (window latency = config-derived buffering floor; pipeline latency =
  measured wall-clock time, `None` for non-realtime batch runs), and nullable
  `ground_truth_zone`/`ground_truth_motion` (never auto-populated by the online
  pipeline — reserved for a future labeled validation harness, Section 23).
  `DatabaseManager` Protocol.
- `persistence/migrations.py`: real, tested, idempotent migration mechanism
  (`schema_migrations` tracking table + ordered migration list) — proven extensible
  via a test that adds a second migration step.
- `persistence/sqlite_manager.py`: `SQLiteDatabaseManager` — 100% parameterized SQL,
  WAL mode, batch insert, `query_by_run_id`.
- Full pipeline persistence test (Sections 1->4->5->6->7->8->9): 461 records round-
  tripped through SQLite with spot-checks against known active/quiet intervals.
- **Same unguarded-zip pattern noted again** (building `EventLogRecord`s from three
  zipped streams) — flagged as worth fixing generically once the section that wires
  this into the live `Application` loop is reached, not fixed yet.

### Section 10 — WebSocket Server
- `contracts/state_message.py`: `StateMessage` — the revised wire contract
  replacing the *original SRS's* proposed JSON payload. Adds `schema_version`
  (starts at 1; original proposal had none), separate `is_motion`/`presence`
  booleans (replacing the ambiguous `"status": "occupied"` string), `separation_margin`
  (replacing a proposed `"confidence"` field), `calibration_version`. Deliberately
  excludes `system_health` (scoped to Section 11 instead, to keep the high-frequency
  state stream focused).
- `communication/websocket_server.py`: `WebSocketStatePublisher` — uses
  `websockets.sync.server`, config-driven host/port (default `127.0.0.1:8765`, not
  hardcoded — the original audit flagged localhost-only as a deployment-flexibility
  risk), multi-client broadcast, disconnect resilience proven with a real abrupt
  client close (not just a mocked test), safe zero-client no-op `publish()`.
- **One doc-only correction applied:** `communication-contracts.md` had incorrectly
  labeled `separation_margin` as specifically "Euclidean distance gap" when the
  actual default metric is `cosine_distance` — corrected to metric-agnostic
  language.
- Full pipeline-to-WebSocket integration test confirmed live client reception
  matching known scenario intervals.

### Section 11 — Health Monitoring
- `contracts/health_status.py`: `SystemHealth` — categorical status vocabulary only
  (`active`/`stale`/`disconnected` for CSI; `connected`/`disconnected`/`unknown` for
  DB; `healthy`/`degraded`/`critical` overall rollup) — **explicitly no fabricated
  numeric health score**, consistent with the `separation_margin`-not-confidence
  discipline established in Section 8. `HealthMonitor` Protocol built around
  registerable probe callables (decoupled from any concrete subsystem).
- `monitoring/health_monitor.py`: `SystemHealthMonitor` — pull model (no background
  polling thread), config-driven `stale_after_seconds`/`disconnected_after_seconds`
  thresholds, strict `<` boundary comparisons confirmed correct at exact threshold
  values. Verified: a probe exception from the **database** specifically maps to
  `DISCONNECTED` (not the softer `UNKNOWN`) so a real DB outage is correctly
  escalated to `CRITICAL` in the rollup, not silently downgraded to `DEGRADED`.
  `UNKNOWN` is reserved for "no probe registered" / "unrecognized status string,"
  not "probe failed."
- Explicitly observation-only: does not implement any reconnection/recovery logic
  (that's each subsystem's own future responsibility).

### Section 12 — Developer Dashboard
- `dashboard/index.html`: self-contained (no build step, no framework, no CDN),
  displays every `StateMessage` field honestly by its real name, configurable
  WebSocket URL input (not hardcoded), client-side auto-reconnect with visible
  connection state, and — verified in the actual JS, not just claimed — genuinely
  clears all displayed values to "—" on disconnect rather than showing stale data as
  current (`resetLiveDisplayToHonestState()`), and dynamically grows its zone
  display based on the highest `zone_id` observed rather than assuming 4 zones.
- `scripts/dashboard_demo.py`: standalone script that runs the full Section
  1->4->5->6->7->8 pipeline into a live `WebSocketStatePublisher`, for manual dashboard
  verification; supports `--zones N` for arbitrary zone counts, `--fast`/`--no-loop`
  for headless test runs.
- `docs/development/dashboard-usage.md`: usage guide; explicitly documents that
  health status is not yet shown (deferred to Section 21) and that zone display has
  no assumed zone count.
- **Correction applied:** a duplicate copy of `dashboard-usage.md` had been created
  under `backend_pipeline/docs/development/` — collapsed to one canonical copy at
  `docs/development/dashboard-usage.md` with a one-line pointer file left in the
  duplicate's place.
- **Minor gap noted, not fixed:** the two `--port 0` (ephemeral port) verification
  runs in the walkthrough only proved the *pipeline* streams correctly headless —
  they did not prove dashboard connectivity, since the dashboard's URL field is not
  auto-populated with an ephemeral port and no human was present to read it from the
  console during those runs. Actual dashboard-to-publisher connectivity is proven
  instead by the automated WebSocket client tests in `test_dashboard_demo.py`, which
  read `publisher.actual_port` programmatically.

## 4. Important Architectural & Technical Decisions

These are decisions made *in conversation* (not just implemented) that a new
assistant should preserve:

- **Occupancy vs. motion is the single most important framing correction in the
  whole project.** The system detects motion (RF variance above threshold); a
  stationary person is not reliably detected. `presence` is an honest,
  timeout-based heuristic layered on top of motion detection — never described or
  implemented as true occupancy sensing.
- **Zone-count discriminability (audit item #10) is treated as an open empirical
  question, not a guaranteed feature.** Section 7's separability measurement exists
  specifically to surface this rather than assume it. Every doc that touches
  synthetic separability results explicitly states they prove the *mechanism*
  works, not that real hardware will discriminate zones the same way.
- **`separation_margin`, never "confidence."** Recurs as an enforced naming rule
  (tested via `dir()`/substring checks) starting at Section 8 and carried through
  Sections 9, 10, and the dashboard.
- **Ground-truth isolation is a hard architectural line.** `CSIRecord` and
  `FeatureVector` never carry zone or motion ground truth; labeling only exists in
  calibration-specific structures (`LabeledFeatureSample`) or test-time external
  computation from known scenario event timing. This preserves parity between
  synthetic and real hardware data paths.
- **Timestamp convention is strictly UTC epoch seconds (`time.time()`)** — this was
  an early ambiguity (`CSIRecord.timestamp` docstring originally said "epoch or
  relative") that was caught and corrected in Section 1, then treated as a fixed
  rule for every timestamped structure since.
- **Every algorithmic parameter is config-driven, never hardcoded** — window sizes,
  thresholds, zone counts, sample rates, subcarrier counts, distance metrics,
  separability thresholds, host/port, staleness thresholds. This is treated as a
  non-negotiable rule throughout, not just an initial preference.
- **Window-fill latency and measured pipeline latency are always kept as two
  distinct, separately-tracked quantities**, never combined into one "latency"
  number — this traces back to a correction of the *original* SRS's vague 1-2s
  latency target, and is enforced in both docs (Section 5) and the DB schema
  (Section 9).
- **Deferred-scope discipline:** several sections explicitly defer real
  wiring/integration to a later section rather than scope-creeping it in — e.g.
  `run_id` generation and live `Application.run()` wiring for both the database
  (Section 9) and WebSocket publisher (Section 10) are deferred to a future
  end-to-end integration section (referred to as Section 21 in `PROJECT_OVERVIEW.md`
  at time of writing); health status broadcasting is likewise deferred. This pattern
  should be respected when writing future prompts — don't let a section quietly
  absorb another section's responsibility.
- **Hardware chip compatibility:** both of the user's physical boards — a
  "WROOM-32X" and a standard "ESP-WROOM-32" — were confirmed (by discussion, not by
  independently-verified datasheet lookup) to be original ESP32 (Xtensa LX6) chip
  family, which is what the CSI toolchain requires. The "X" suffix was explained as
  a module/antenna variant, not a different chip.
- **ESP-IDF version pinned to v5.2.8** (installed and working on Windows, submodule
  clone issues resolved via `git submodule update --init --recursive --depth 1 -j 1`
  after initial network-drop failures). This was flagged as an EOL/soon-to-be-EOL
  branch at the time of the version-choice discussion (recommendation at the time
  was v5.4.x for a currently-supported branch), but the user had already begun
  installing v5.2.8 by the time this was raised, and proceeded with it rather than
  restarting on a different version. **No further action was taken on this — it's
  an accepted tradeoff, not an unresolved bug**, but a new assistant should know the
  installed IDF version is not on Espressif's currently-supported branch list.
- **Plan for using official Espressif examples (`examples/wifi/csi/csi_recv` and
  `csi_send`) rather than a third-party CSI toolkit**, specifically to avoid the
  IDF-version-compatibility risk that third-party tools carry.

## 5. Current Repository Structure (as established across all prompts)

```
WiSense-Twin/
  PROJECT_OVERVIEW.md
  PROJECT_HANDOFF.md              <- this file
  dashboard/
    index.html
  scripts/
    dashboard_demo.py
  docs/
    development/
      dashboard-usage.md          <- canonical copy
  backend_pipeline/
    main.py
    requirements.txt
    src/wisense_backend/
      contracts/
        csi_source.py
        preprocessor.py
        feature_extractor.py
        motion_detector.py
        calibration.py
        zone_classifier.py
        database.py
        state_message.py
        websocket_publisher.py
        health_status.py
      config/
        app_config.py
      ingestion/
        synthetic_source.py
      preprocessing/
        hampel_filter.py
        moving_average.py
        pipeline.py
      features/
        sliding_window.py
        variance_extractor.py
      detection/
        threshold_motion_detector.py
      calibration/
        labeled_sample.py
        calibrator.py
        separability.py
      classification/
        nearest_signature_classifier.py
      persistence/
        migrations.py
        sqlite_manager.py
      communication/
        websocket_server.py
      monitoring/
        health_monitor.py
      app.py
      logging_setup.py
    config/
      system_config.json
      preprocessing_config.json
      feature_extraction_config.json
      motion_detection_config.json
      calibration_profiles/
        default_calibration.json
      zone_classification_config.json
      database_config.json
      websocket_config.json
      health_monitor_config.json
      synthetic_scenarios/
        example.json
    docs/
      development/
        configuration.md
        testing-strategy.md
        dashboard-usage.md       <- one-line pointer to canonical copy above
      signal-processing/
        pipeline.md
        feature-engineering.md
        motion-detection.md
        calibration-methodology.md
        classification-strategy.md
      architecture/
        database-architecture.md
        communication-contracts.md
        health-monitoring.md
    tests/
      test_app_config.py
      test_app_skeleton.py
      test_synthetic_source.py
      test_hampel_filter.py
      test_moving_average.py
      test_preprocessing_pipeline.py
      test_sliding_window.py
      test_variance_extractor.py
      test_threshold_motion_detector.py
      test_calibrator.py
      test_separability.py
      test_nearest_signature_classifier.py
      test_migrations.py
      test_sqlite_manager.py
      test_state_message.py
      test_websocket_server.py
      test_health_monitor.py
      test_logging_setup.py
      test_dashboard_demo.py
  frameworks/                     <- [UNKNOWN exact final path] ESP-IDF v5.2.8
                                      installed under C:\Expressif\frameworks\
                                      esp-idf-v5.2.8 (outside the WiSense-Twin repo
                                      tree, per the terminal session shown)
```

**Note:** `esp32_firmware/` (transmitter/receiver firmware directories) does not
exist yet — Section 13/14 have not been started. A separate ad-hoc spike directory
`C:\wisense-spike\csi_recv` (and presumably a parallel `csi_send` copy) was proposed
for the hardware feasibility spikes, **outside** the main repo, specifically so
spike/throwaway code doesn't get mixed into the real firmware sections later.
Whether that spike directory was actually created and used is **[UNKNOWN]**.

## 6. Code Written or Modified

Full source code for every file listed in §3/§5 was written by Antigravity and
pasted into the conversation with the previous assistant across many turns. **That
full source is not reproduced in this handoff file** (it would be extremely long and
largely redundant with what's already in the actual repository on the user's
machine). If continuing this project, **the actual repository files themselves are
the source of truth** — read them directly rather than relying on this document's
summaries for anything code-level. This document is a map, not a mirror.

## 7. Tests Run and Their Results

Cumulative `pytest` pass counts, as reported after each section (each run reported
zero regressions in prior sections' tests):

| After Section | Test count | Notes |
|---|---|---|
| 2 & 3 | 44 passed | includes the not-yet-corrected stop-signal test |
| 4 | 66 passed | |
| 5 | 79 passed | |
| 6 | 89 passed | |
| 7 | 111 passed -> 112 after the immutability correction | new regression test added |
| 8 | 124 passed | |
| 9 | 140 passed | |
| 10 | 171 passed | |
| 11 | 195 passed | |
| 12 | 199 passed | |

All numbers above are as **reported by Antigravity's walkthroughs**; the previous
assistant cross-checked several of them arithmetically (e.g. confirming
124 = 112 + 12, 171 = 140 + 31) and cross-checked specific test *behavior* against
pasted source code in most sections, but did **not** independently execute the test
suite itself (no code execution tooling was used in that conversation — all
verification was via manual code review of pasted source).

## 8. Known Bugs, Errors, and Unresolved Issues

1. **Stop-signal test (Sections 2/3):** user confirmed fixed in words
   ("the stop signal is fixed"), but the corrected source code was never re-pasted
   for independent verification. **Recommend a spot-check** of
   `test_app_skeleton.py::test_stop_signal_shuts_down_cleanly` before relying on it.
2. **Unguarded positional `zip()` across parallel streams** — flagged twice as a
   non-blocking hardening note, not yet fixed:
   - `NearestSignatureZoneClassifier.classify_stream()` (Section 8) zips `features`
     and `motion_states` with no correspondence check.
   - The Section 9 integration test's `EventLogRecord` construction loop zips three
     streams (`feature_vectors`, `motion_states`, `classifications`) the same way.
   Suggested fix (not yet applied): an assertion that timestamps match across
   streams before pairing, or `zip(..., strict=True)` (Python 3.10+) at minimum.
   Should probably be addressed generically when the future end-to-end integration
   section is implemented, since that's where these streams get wired into the live
   `Application` loop for the first time.
3. **Dashboard ephemeral-port verification gap (Section 12):** the two `--port 0`
   headless verification runs in the walkthrough prove the *pipeline* runs
   end-to-end, but do not prove dashboard-to-publisher connectivity (the dashboard's
   URL field isn't auto-populated with the ephemeral port). Actual connectivity
   proof rests on the automated `test_dashboard_demo.py` WebSocket client tests
   instead. Not a bug requiring a fix, but worth knowing this specific claim in the
   walkthrough was slightly overstated relative to what those two commands alone
   demonstrated.
4. **ESP32 hardware spike results: [UNKNOWN].** The user was given full step-by-step
   instructions to (a) confirm both boards report chip type "ESP32" via
   `esptool.py chip_id`, (b) flash the official `csi_recv`/`csi_send` examples to
   each board, (c) confirm real structured CSI data appears in the receiver's
   monitor output, and (d) measure real achieved throughput/drop rate over a timed
   window. **None of these results were reported back before this handoff.** This
   is the single most important open item — Section 13 (ESP32 transmitter firmware)
   should not be started as originally planned until these spike results are known,
   per the previous assistant's explicit recommendation.
5. **ESP-IDF v5.2.8 is on an EOL/soon-to-be-EOL branch** (as of the date this was
   discussed). This was accepted as a tradeoff rather than fixed (see §4). Not
   currently broken, but worth being aware of if any hard-to-diagnose build issue
   comes up later — it may be worth checking Espressif's current supported-branch
   list again before doing serious firmware work.
6. **No independent code execution occurred in the source conversation** — all
   test-count and behavior verification was done by the previous assistant reading
   pasted source code and walkthrough text, not by running anything itself. If the
   new environment has code-execution capability, an early useful step would be to
   actually open the real repository and run `pytest` directly to confirm the
   199 passed count and general repo health independently.

## 9. Research Sources, References, or Evidence

- **No external URLs, papers, or citations were referenced or fetched during the
  conversation.** All architectural guidance (ESP32 CSI hardware compatibility,
  ESP-IDF versioning behavior, WiFi CSI motion-vs-occupancy limitations, Hampel/MAD
  filtering theory, cosine-distance separability reasoning) was provided from the
  assistant's general domain knowledge, not from a specific cited source.
- The user did perform a live installation process (real terminal commands: `git
  clone`, the ESP-IDF installer, `pip install`) — the terminal output from that real
  installation (ESP-IDF v5.2.8 successfully installed, submodule clone recovered
  after network failures) is the only "evidence" in the strict sense that was pasted
  into the conversation, and it's reproduced faithfully in §4 above.
- The original project brief (uploaded as a document at the start of the
  conversation, titled "WiSense-Twin SRS/master-prompt") and an accompanying SRS PDF
  were the two source documents the entire audit and roadmap were built from. If
  those original files are available in the new environment, they are the ultimate
  source of truth for requirements — **[UNKNOWN]** whether they will be
  re-uploaded/available going forward.

## 10. Exact Next Steps for Continuing the Work

In order of priority:

1. **Get a definitive answer on the ESP32 hardware spike status** from the user:
   did real CSI data appear in the receiver's serial monitor, and what was the
   measured throughput/drop rate? This directly determines what Section 13's
   implementation prompt should assume (binary vs. text serial framing, confirmed
   working IDF/example combination, etc.).
2. **If the spike hasn't been run yet**, walk the user through it before proceeding
   to Section 13 — steps are: confirm chip identity via `esptool.py chip_id` on both
   boards, flash `%IDF_PATH%\examples\wifi\csi\csi_recv` to one board and
   `csi_send` to the other, confirm structured CSI data appears in the monitor
   output, then capture ~60s of monitor output to a log file and compute
   records/sec and drop rate against the transmitter's configured packet rate.
3. **Optionally spot-check the Section 2/3 stop-signal fix** by asking for the
   current `test_app_skeleton.py` source, since it was never independently
   re-verified after the correction.
4. **Once spike results are known, write the Section 13 prompt** (ESP32 transmitter
   firmware) — following the established prompt pattern: read
   `PROJECT_OVERVIEW.md` first, scope tightly, config-driven parameters only,
   explicit Definition of Done, and — per the standing instruction added at Section
   12 — require full file contents pasted directly into the walkthrough.
5. **Continue the roadmap in order** from `PROJECT_OVERVIEW.md`'s section table:
   14 (ESP32 CSI receiver firmware), 15 (real hardware integration — swapping
   `SyntheticCSISource` for a real serial-based `CSIRecordSource` implementation,
   which is the point where all the ground-truth-isolation and interface-generic
   design work from Sections 1-12 should pay off), 16-20 (Unreal Engine 5 work),
   21 (end-to-end integration — this is where `run_id` generation, live
   `Application` wiring for the database and WebSocket publisher, and health-status
   broadcasting are all deferred to, per multiple sections' explicit scope
   statements), 22 (performance testing), 23 (experimental validation — this is
   where the nullable `ground_truth_zone`/`ground_truth_motion` fields in
   `EventLogRecord` finally get used), 24 (final documentation), 25 (final
   demonstration prep).
6. **Keep applying the same review discipline** established throughout this
   conversation: don't accept a walkthrough's claims at face value — request and
   read actual source code, cross-check test counts arithmetically, verify
   specifically-claimed behaviors (e.g. "X is now immutable," "Y never appears
   anywhere") against the real implementation rather than the description of it,
   and use small, tightly-scoped correction prompts rather than reopening whole
   sections when something needs fixing.
7. **Address the two open unguarded-zip hardening items** (see §8.2) at whatever
   point the end-to-end integration section is written, since that's the natural
   place where these streams get wired together for real.
