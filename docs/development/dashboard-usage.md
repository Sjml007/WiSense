# Developer Dashboard — Usage Guide & Verification Manual

## 1. Overview

The **WiSense-Twin Developer Dashboard** (`dashboard/index.html`) is a self-contained, browser-based telemetry inspector. It connects via WebSocket to the backend's `WebSocketStatePublisher` (Section 10) to observe real-time spatial motion detection, presence state, active zone classification, and separation margins.

### Key Architectural Principles
- **Zero-Build & Dependency-Free**: Plain HTML, Vanilla CSS, and native browser JavaScript in a single self-contained file (`dashboard/index.html`). No node_modules, bundlers, JS frameworks, or CDN resources. Operates completely offline.
- **Strict Wire-Contract Transparency**: Displays every field from the canonical `StateMessage` contract honestly and by its real name (`is_motion`, `presence`, `zone_id`, `separation_margin`, `calibration_version`, `schema_version`).
- **Honest Disconnection Handling**: When disconnected from the publisher, active telemetry values are immediately cleared to `"—"` and an explicit disconnection banner is displayed. Last-known numbers are never presented as live data.
- **Configurable Endpoint**: The WebSocket URL is configured directly within the page input field rather than hardcoded.

---

## 2. Quick Start: Manual Verification Workflow

Since there is no formal frontend testing framework in this project, the end-to-end verification path uses `scripts/dashboard_demo.py`. This script pipes a full multi-zone synthetic scenario through the real 8-stage pipeline:

$$\text{SyntheticCSISource (20 Hz)} \longrightarrow \text{Preprocessing} \longrightarrow \text{Feature Extraction} \longrightarrow \text{Motion Detector} \longrightarrow \text{Classifier} \longrightarrow \text{WebSocket Publisher}$$

### Step 1: Launch the Streaming Publisher
From the project root directory, run:

```bash
python scripts/dashboard_demo.py --port 8765
```

The script will output startup instructions and begin streaming paced frames:

```text
======================================================================
  WiSense-Twin Developer Dashboard Verification Demo (Section 12)
======================================================================
  WebSocket Endpoint: ws://127.0.0.1:8765
  Dashboard File:     .../WiSense-Twin/dashboard/index.html
  Instructions:
    1. Open dashboard/index.html in your web browser
    2. Ensure endpoint field is set to: ws://127.0.0.1:8765
    3. Click 'Connect' to observe real-time StateMessage telemetry
    4. Press Ctrl+C in this terminal to stop cleanly
======================================================================
```

### Step 2: Open the Dashboard in a Web Browser
Open `dashboard/index.html` directly in any modern browser (Chrome, Firefox, Edge, Safari):

- Via file manager / double-click: `file:///path/to/WiSense-Twin/dashboard/index.html`
- Or from the terminal (Windows):
  ```powershell
  start dashboard/index.html
  ```

### Step 3: Connect & Inspect
1. Verify the endpoint input is `ws://127.0.0.1:8765`.
2. The dashboard automatically attempts connection on load; you may also click **Connect**.
3. Confirm the status pill transitions from `Disconnected` to `CONNECTED (ws://127.0.0.1:8765)` with an active green pulse.
4. Watch the pipeline timeline cycle through quiet baselines, active motion in zones 1 through 4, and presence decay tails.

---

## 3. Telemetry Field Reference & Invariant Rules

| Dashboard Field | Wire Contract Name | Permitted Values | Behavioral Specification |
|---|---|---|---|
| **Instantaneous Motion** | `is_motion` | `true`, `false` | True when subcarrier variance exceeds `enter_threshold` ($0.10$); False when it drops below `exit_threshold` ($0.05$) via dual-threshold hysteresis. |
| **Sustained Presence** | `presence` | `true`, `false` | Remains True while motion is detected, and persists across a 2.0-second decay timeout after motion ceases. |
| **Spatial Zone ID** | `zone_id` | `1`, `2`, ..., $N$, or `null` | 1-indexed room zone. **Strictly motion-gated**: when `is_motion` is False, `zone_id` is always `null` (rendered as `"—"`). Never fabricated during quiet periods. |
| **Separation Margin** | `separation_margin` | Float $\ge 0.0$, or `null` | Distance gap ($d_2 - d_1$) between the closest zone signature and runner-up. **Labeled strictly `separation_margin` — NEVER "confidence"**. `null` when `is_motion` is False. |
| **Calibration Version** | `calibration_version` | Integer $\ge 1$, or `null` | Provenance identifier of the active `CalibrationProfile`. Rendered as `v1`. |
| **Schema Version** | `schema_version` | Integer $\ge 1$ | Wire protocol version (starts at 1). Informs downstream consumers (e.g. UE5 client) of message schema. |
| **Timestamp** | `timestamp` | Epoch float | UTC Unix epoch seconds. Dashboard calculates transmission latency against local browser clock. |

---

## 4. Current Limitations & Architectural Boundaries

### 4.1 Health Status Telemetry is Deferred to Section 21
> [!IMPORTANT]
> **SystemHealth is Not Displayed in This Section**
> In Section 11, the `HealthMonitor` and `SystemHealth` contract were implemented for background diagnostic probing. However, `SystemHealth` is intentionally decoupled from the high-frequency 20 Hz spatial `StateMessage` stream.
> 
> As established by the project roadmap and architecture, `SystemHealth` is not yet broadcast over any network channel. Broadcasting and displaying health status is formally deferred to **Section 21 (End-to-end integration)** when the complete Application runtime is wired.
> 
> The dashboard in Section 12 exclusively displays the live `StateMessage` stream from Section 10.

### 4.2 Dynamic Zone Discovery (No Assumed Zone Count)
> [!NOTE]
> **Dynamic Zone Rendering**
> The WiSense-Twin system constraint mandates that room partition count ($N$) is configurable and never hardcoded to 4 zones.
> 
> Because the wire contract (`StateMessage`) conveys only the currently active `zone_id` (and does not include `zone_count`), the dashboard **does not assume a fixed 4-zone layout**. Instead:
> 1. The dashboard displays `zone_id` as a direct, plain value (`Zone 1`, `Zone 2`, etc., or `"—"`).
> 2. The dynamic zone visualization bar discovers zones incrementally, rendering indicator pills up to the highest `zone_id` observed during the streaming session.
> 3. If a 6-zone scenario is streamed, the dashboard will dynamically discover and display all 6 zone pills without any dashboard code modifications.

---

## 5. Demo Script CLI Options

`scripts/dashboard_demo.py` provides several flags for testing edge cases:

```text
usage: dashboard_demo.py [-h] [--host HOST] [--port PORT] [--sample-rate SAMPLE_RATE]
                         [--zones ZONES] [--subcarriers SUBCARRIERS]
                         [--calibration-profile CALIBRATION_PROFILE] [--no-loop]

options:
  -h, --help            show this help message and exit
  --host HOST           Host address to bind WebSocket publisher to (default: 127.0.0.1)
  --port PORT           TCP port to bind WebSocket publisher to (default: 8765)
  --sample-rate SAMPLE_RATE
                        Sampling rate in Hz for synthetic frame generation (default: 20.0)
  --zones ZONES         Number of spatial zones in synthetic scenario (default: 4)
  --subcarriers SUBCARRIERS
                        Subcarrier count per CSI record (default: 64)
  --calibration-profile CALIBRATION_PROFILE
                        Optional path to CalibrationProfile JSON file (auto-calibrates if omitted)
  --no-loop             Run scenario once instead of looping continuously (default: False)
```

### Examples

- **Test arbitrary zone counts (e.g. 6 zones)**:
  ```bash
  python scripts/dashboard_demo.py --zones 6
  ```
  The script automatically generates a 6-zone scenario, calibrates reference profiles for 6 zones on the fly, and streams them. The dashboard will dynamically expand its zone bar to 6 zones.

- **Single pass without looping**:
  ```bash
  python scripts/dashboard_demo.py --no-loop
  ```

- **Ephemeral or custom port**:
  ```bash
  python scripts/dashboard_demo.py --port 9001
  ```
  (Enter `ws://127.0.0.1:9001` in the dashboard endpoint field to connect.)
