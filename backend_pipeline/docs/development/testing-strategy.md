# Testing Strategy & Synthetic Data in the SDLC

## 1. Role in the Software Development Life Cycle (SDLC)

### 1.1 Why Synthetic-First?
Developing device-free WiFi sensing systems presents unique testing and engineering challenges:
- **Physical RF Environment Volatility**: Real-world wireless propagation varies based on humidity, furniture arrangement, multipath reflections, and background 2.4/5 GHz interference.
- **Human Labor Bottleneck**: Verifying an algorithm or testing a bug by physically walking around a room 50 times is slow, error-prone, and impossible in continuous integration (CI) environments.
- **Hardware Supply & Firmware Decoupling**: Hardware development (ESP32 transmitter/receiver firmware, UART drivers) frequently lags backend pipeline development or encounters component delivery delays.

The **Synthetic CSI Data Generator** solves these problems by providing an automated, mathematically sound, deterministic simulation of WiFi CSI disturbance patterns.

### 1.2 What Synthetic-First Unblocks
Building the synthetic generator first unblocks the entire downstream roadmap:

1. **Preprocessing Pipeline (Section 4)**: Filters (e.g., Hampel outlier filters, low-pass Butterworth smoothers) can be evaluated against precise signal-to-noise ratios (SNRs) with known noise distributions.
2. **Feature Extraction (Section 5)**: Variance computation, spectral Doppler energy estimation, and subcarrier correlation metrics can be verified against known injected oscillation frequencies.
3. **Motion Detection & Zone Classification (Sections 6 & 8)**: Detectors and classifiers can be benchmarked against 100% accurate ground-truth timelines (e.g., "motion in Zone 2 from $t=5\text{s}$ to $t=10\text{s}$"). Accuracy, confusion matrices, and detection latencies can be calculated automatically.
4. **WebSocket Streaming & Unreal Engine 5 Digital Twin (Sections 10 & 16-20)**: The UE5 team can build and polish real-time 3D room avatars, zone highlight shaders, and camera interactions with a live 20 Hz streamed scenario, months before physical ESP32 transceivers are deployed in the room.
5. **Continuous Integration & Zero-Regression CI**: Automated tests run in milliseconds (`realtime=False`), executing hundreds of simulated frames without requiring hardware or wall-clock delays.

---

## 2. Scenario Configuration Schema

Scenario files are stored in JSON format under `config/synthetic_scenarios/` (such as `example.json`).

### 2.1 Complete Schema Specification

```json
{
  "scenario_name": "string (unique identifier)",
  "description": "string (human readable explanation)",
  "sample_rate_hz": "float (> 0.0, e.g. 20.0)",
  "subcarrier_count": "integer (> 0, e.g. 64)",
  "zone_count": "integer (>= 1, e.g. 4)",
  "duration_seconds": "float (>= 0.0, e.g. 15.0; 0.0 denotes infinite stream)",
  "realtime": "boolean (true for wall-clock paced playback, false for fast-forward)",
  "baseline_amplitude": "float (nominal static CSI amplitude, e.g. 30.0)",
  "noise_std": "float (standard deviation of thermal Gaussian noise, e.g. 0.35)",
  "baseline_rssi": "float (nominal baseline RSSI in dBm, e.g. -46.0)",
  "channel": "integer (WiFi RF channel index, e.g. 6)",
  "random_seed": "integer or null (seed for deterministic reproducibility)",
  "events": [
    {
      "start_time_s": "float (>= 0.0, start of motion event)",
      "end_time_s": "float (> start_time_s, end of motion event)",
      "zone_id": "integer (1-indexed: 1 <= zone_id <= zone_count)",
      "motion_type": "string ('walking' or 'subtle')",
      "intensity": "float (>= 0.0, scaling factor for disturbance)"
    }
  ]
}
```

### 2.2 Field Descriptions

| Field | Type | Description |
|---|---|---|
| `scenario_name` | `string` | Identifier used in logging and test reporting. |
| `sample_rate_hz` | `float` | Ingestion frequency in frames per second (Hz). Real values come from ESP32 spikes; config-driven. |
| `subcarrier_count` | `int` | Number of OFDM subcarriers captured per packet (typically 52 or 64 in 20 MHz channels). |
| `zone_count` | `int` | Number of logical physical zones in the monitored space. Never hardcoded. |
| `duration_seconds`| `float` | Total duration of the scenario. |
| `realtime` | `bool` | When `true`, generator sleeps between frames to match wall-clock time. When `false`, generates instantly. |
| `baseline_amplitude` | `float` | Center amplitude around which static multipath profile is structured. |
| `noise_std` | `float` | Gaussian thermal noise level $\sigma$. |
| `baseline_rssi` | `float` | Nominal received signal strength indicator in dBm. |
| `random_seed` | `int \| null` | Set to an integer (e.g. 42) for 100% reproducible test vectors. |
| `start_epoch_time` | `float \| null` | Optional UTC Unix epoch timestamp for scenario start. Defaults to `time.time()` at runtime. |
| `events` | `list` | Chronological or overlapping schedule of motion events. |
| `events[i].zone_id` | `int` | Active zone (1 to $N$). |
| `events[i].intensity` | `float` | Amplitude perturbation scale. $1.0 = \text{standard human walking}$. |

> [!IMPORTANT]
> **Timestamp Convention**: Every `CSIRecord.timestamp` is strictly formatted in UTC Unix epoch seconds (`time.time()`). Relative clocks and arbitrary counter offsets are not permitted in records, ensuring 100% parity with hardware serial readers.

---

## 3. Physical Signal Simulation Model

The generator employs a plausible, illustrative multipath model, not a validated real-world signature. The zone-to-multipath mapping used here is a simplification for testing purposes rather than an empirically validated hardware signature:

1. **Static Multipath Base Profile**: In undisturbed rooms, stationary walls and objects create constructive/destructive interference, imparting a slight frequency-selective ripple across subcarriers.
2. **Thermal & Background Noise**: Additive White Gaussian Noise (AWGN) $\mathcal{N}(0, \sigma^2)$ across all subcarriers.
3. **Illustrative Zone Spatial Response**:
   For any configured $N$ zones, subcarriers are mapped across the spectrum as a testing heuristic:
   $$c_z = \left(\frac{z - 0.5}{N}\right) \cdot N_{sc}, \quad \sigma_z = \max\left(2.5, \frac{N_{sc}}{2N}\right)$$
   Subcarriers near $c_z$ receive a sensitivity weight:
   $$w_z(k) = \exp\left( -\frac{(k - c_z)^2}{2\sigma_z^2} \right)$$
   When motion occurs in zone $z$:
   - A Doppler modulation wave ($\sim 3.5\text{ Hz}$ for walking, $\sim 1.8\text{ Hz}$ for subtle motion) perturbs subcarrier amplitudes.
   - Dynamic scattering turbulence increases variance specifically in zone $z$'s subcarrier band.
   - Signal shadowing subtly attenuates RSSI ($\sim 1.5\text{ dBm}$ drop).

---

## 4. Execution & Verification Guide

### 4.1 Running the Automated Test Suite
From `backend_pipeline/`:
```bash
python -m pytest -v
```

### 4.2 Standalone Execution & Scenario Replay
Run the standalone runner script with the example scenario:
```bash
python run_synthetic.py --config config/synthetic_scenarios/example.json
```

To run with real-time wall-clock pacing (simulating live ESP32 ingestion at 20 Hz):
```bash
python run_synthetic.py --config config/synthetic_scenarios/example.json --realtime
```

To view full frame-by-frame debug statistics:
```bash
python run_synthetic.py --config config/synthetic_scenarios/example.json --verbose --limit 20
```
