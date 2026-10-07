# WiSense-Twin — ESP32 Transmitter Firmware (`tx_beacon`)

## 1. Executive Summary & Purpose

During early hardware exploratory trials, three baseline captures (`walking_01.txt`, `enter_leave_01.txt`, `person_still_01.txt`) were collected against an unverified transmitter configuration. Comprehensive timing analysis of those captures revealed:
- An irregular effective frame rate averaging ~70 Hz (rather than a steady 100 Hz).
- High frame-to-frame timing jitter (standard deviation of 8–12 ms, with intermittent gaps up to 130 ms).
- A severe **11.9-second total transmission stall** observed in `walking_01.txt`.

These symptoms are characteristic of uncontrolled ambient WiFi traffic, aggressive power-saving radio sleep states, or severe task starvation, rather than a calibrated sensing beacon.

**Section 13 implements the dedicated WiSense transmitter firmware (`tx_beacon`) to permanently eliminate this ambiguity.** By compiling and flashing our own controlled firmware onto the transmitting ESP32, we guarantee:
1. Every transmitted packet has a deterministic sequence number, timestamp, and metadata payload.
2. The packet transmission rate and WiFi channel are known, strictly enforced, and configurable.
3. Ambient RF traffic is isolated from our sensing channel.
4. Future CSI calibration recordings (Section 15) operate against an auditable, reproducible ground-truth RF emitter.

---

## 2. Hardware & Toolchain Specifications

| Parameter | Specification | Verification Note |
| :--- | :--- | :--- |
| **Target Microcontroller** | Espressif ESP32 (dual-core Xtensa LX6) | Confirmed via `chip_id` during initial hardware spikes |
| **ESP-IDF Version** | **v5.2.8** | Installed at `C:\Expressif\frameworks\esp-idf-v5.2.8` |
| **Target Toolchain** | `xtensa-esp32-elf` (GCC 13.2.0) | Bundled in `.espressif/tools` |
| **Python Environment** | Python 3.14 (`idf5.2_py3.14_env`) | ESP-IDF virtual environment |
| **WiFi MAC Address** | `1a:00:00:00:00:00` (Fixed STA MAC) | Matches `CONFIG_CSI_SEND_MAC` filter in CSI receiver |
| **Destination MAC** | `ff:ff:ff:ff:ff:ff` (Broadcast) | Unicast overhead avoided; raw frame reception |

---

## 3. Configuration & Kconfig Architecture

In adherence to the project-wide constraint prohibiting hardcoded operational parameters, all transmission and RF parameters are managed via `Kconfig.projbuild` and configurable via `idf.py menuconfig`.

```text
WiSense Transmitter Configuration
  (100) Transmission rate (packets/second)
  (11)  WiFi Channel (1-13)
  (1)   Serial status log interval (seconds)
  (32)  Payload size (bytes)
```

### Parameter Rationale

1. **`CONFIG_TX_BEACON_PACKET_RATE` (Default: `100` pps / 100 Hz)**
   - *Rationale*: Human body motion, gait dynamics, and limb movements generate Doppler shifts and multipath fluctuations up to 20–30 Hz. To satisfy the Nyquist sampling criterion with safety margin, a transmission frequency $\ge 50$ Hz is required. A rate of 100 Hz provides high-fidelity Doppler spectra while remaining well within ESP-NOW bandwidth limits. At 1000 Hz FreeRTOS tick rate (`CONFIG_FREERTOS_HZ=1000`), 100 pps aligns with an exact 10 ms cycle, minimizing timer quantization error.
   - *Configurability*: Settable from 1 to 500 pps to allow empirical comparison with 50 pps or 20 pps modes.

2. **`CONFIG_TX_BEACON_WIFI_CHANNEL` (Default: `11`)**
   - *Rationale*: In the 2.4 GHz ISM band, channels 1, 6, and 11 are non-overlapping 20 MHz channels. Channel 11 is selected based on lower measured co-channel interference during our laboratory spike trials and directly matches the channel used during the initial hardware feasibility spikes.
   - *Configurability*: Settable from 1 to 13.

3. **`CONFIG_TX_BEACON_LOG_INTERVAL_SEC` (Default: `1` second)**
   - *Rationale*: Periodic status reporting allows a human operator (or an automated test harness) to immediately verify achieved transmission frequency and heap stability in real time without requiring the paired CSI receiver.

4. **`CONFIG_TX_BEACON_PAYLOAD_SIZE` (Default: `32` bytes)**
   - *Rationale*: Sufficient to pack the 32-bit packet sequence number (`seq_num` at byte offset 0, matching `rx_id` in `csi_recv`), millisecond timestamp, configured rate, channel, and magic header.

---

## 4. Firmware Architecture & Deterministic Timing Engine

The transmitter firmware resides in `esp32_firmware/tx_beacon/` and follows standard ESP-IDF structure:

```text
esp32_firmware/
  tx_beacon/
    CMakeLists.txt          # Root ESP-IDF project declaration
    sdkconfig.defaults      # 1000Hz ticks, AMPDU disabled, dynamic buffers
    main/
      CMakeLists.txt        # Component registration
      Kconfig.projbuild     # Build-time menuconfig schema
      main.c                # Deterministic transmission task & WiFi/ESP-NOW init
```

### Key Architectural Decisions

1. **Station Mode with Disabled Power Save (`WIFI_PS_NONE`)**:
   By default, 802.11 stations periodically sleep their RF front-end between DTIM beacon intervals (Modem-sleep / Light-sleep). We call `esp_wifi_set_ps(WIFI_PS_NONE)` to keep the RF transceiver continuously powered, preventing sporadic packet buffering and transmission jitter.

2. **AMPDU Disabled (`CONFIG_ESP_WIFI_AMPDU_TX_ENABLED=n`)**:
   Packet aggregation (A-MPDU) merges multiple layer-3 frames into a single physical layer transmission. Disabling A-MPDU ensures that every transmitted packet generates an independent physical preamble, producing a distinct Channel State Information estimate on the receiver.

3. **Hybrid Microsecond Precision Pacing**:
   Standard `vTaskDelay()` introduces cumulative drift because execution overhead inside the task loop delays subsequent calls. Naive busy-spinning consumes 100% of a core, risking watchdog triggers and starving the WiFi PHY driver.
   `tx_beacon` implements a hybrid pacing engine:
   - It maintains a running 64-bit microsecond schedule: `next_tx_time_us += target_interval_us`.
   - If the remaining sleep duration exceeds 2 ms, it yields via `vTaskDelay()` to allow FreeRTOS idle tasks and watchdog timers to run.
   - For the sub-millisecond tail, it executes a tight microsecond spin against the hardware high-resolution timer (`esp_timer_get_time()`).
   - This delivers zero cumulative drift and sub-millisecond jitter across arbitrary run lengths.

4. **Core Pinning**:
   The transmission task is pinned to **Core 1** (`xTaskCreatePinnedToCore`), leaving **Core 0** fully dedicated to the ESP-IDF WiFi MAC/PHY interrupt service routines.

5. **Fixed Identity Anchor**:
   The transmitter MAC address is explicitly set to `1a:00:00:00:00:00` via `esp_wifi_set_mac(WIFI_IF_STA, s_tx_mac)`. The paired CSI receiver filters for this exact address (`memcmp(info->mac, CONFIG_CSI_SEND_MAC, 6) == 0`), rejecting all extraneous ambient WiFi frames.

---

## 5. Build, Flash, and Monitor Sequence

To build and flash the firmware using ESP-IDF v5.2.8 on Windows:

### Step 1: Initialize ESP-IDF Environment
Open PowerShell and source the ESP-IDF environment export script:
```powershell
. C:\Expressif\frameworks\esp-idf-v5.2.8\export.ps1
```

### Step 2: Navigate to Project Directory
```powershell
cd "c:\Users\mohan\Desktop\Academic\Software\WiSense Twin\esp32_firmware\tx_beacon"
```

### Step 3: Configure Target (First-time only)
```powershell
idf.py set-target esp32
```

### Step 4: Optional Configuration Adjustment
To change packet rate (e.g., from 100 to 50 pps) or channel:
```powershell
idf.py menuconfig
# Navigate to: "WiSense Transmitter Configuration"
```

### Step 5: Build, Flash, and Open Serial Monitor
Identify the COM port of the transmitter ESP32 board (e.g., `COM3`, `COM4`):
```powershell
idf.py -p COMx build flash monitor
```
*(Press `Ctrl+]` to exit the monitor).*

---

## 6. Verification & Stability Testing

### Verification Mechanism: Periodic Serial Telemetry
The firmware logs a structured telemetry line to the serial monitor once every `CONFIG_TX_BEACON_LOG_INTERVAL_SEC` (default: 1 second):

```text
I (1045) tx_beacon: ================ WiSense TX Beacon Starting ================
I (1045) tx_beacon: Target rate: 100 pps (interval: 10000 us) | Channel: 11 | Payload: 32 bytes
I (2045) tx_beacon: [TX_STAT] count=100 | window_pkts=100 | uptime=2045 ms | rate=100.0 pps (target=100) | heap=287420
I (3045) tx_beacon: [TX_STAT] count=200 | window_pkts=100 | uptime=3045 ms | rate=100.0 pps (target=100) | heap=287420
I (4045) tx_beacon: [TX_STAT] count=300 | window_pkts=100 | uptime=4045 ms | rate=100.0 pps (target=100) | heap=287420
...
I (61045) tx_beacon: [TX_STAT] count=6000 | window_pkts=100 | uptime=61045 ms | rate=100.0 pps (target=100) | heap=287420
```

### 60-Second Standalone Stability Criteria
The transmitter must be run standalone for a minimum of 60 seconds with the following pass criteria:
1. **Zero Resets / Panics**: No FreeRTOS task watchdog triggers, stack overflows, or Brownout detectors.
2. **Deterministic Frame Rate**: Exactly $60 \times 100 = 6000$ packets emitted over 60 seconds ($\pm 0.1\%$ rate variation).
3. **No Memory Leak**: `free_heap` remains completely flat after initialization.
4. **No Transmission Stalls**: No gaps $\ge 50$ ms. Specifically, the 11.9-second stall observed in `walking_01.txt` must not recur.
