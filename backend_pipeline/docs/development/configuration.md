# System Configuration Schema

This document details the configuration schema for the WiSense-Twin backend pipeline, defined in `config/system_config.json` and validated by `AppConfig` (`src/wisense_backend/config/app_config.py`).

---

## 1. Overview & Architectural Principles

1. **Config-Driven Design**: No system dimensions, connection strings, communication ports, or logging parameters are hardcoded anywhere in the codebase.
2. **Immutability**: Runtime configuration is parsed into a frozen `AppConfig` dataclass, ensuring settings cannot be modified once the backend initializes.
3. **Fail-Fast Validation**: All values are strictly validated during initialization (`__post_init__`). Invalid types, port numbers out of range (1–65535), unknown source modes, or missing required files immediately halt startup with clear diagnostic error messages.
4. **Zero COM Port Defaults**: Hardware serial settings (`serial_port`, `serial_baud_rate`) default to `null` to comply with project constraints prohibiting temporary or hardcoded COM port defaults.
5. **Future-Proof Schema**: Fields reserved for upcoming pipeline sections (`websocket_*`, `database_path`, `serial_*`) are defined now, ensuring the configuration contract remains stable across development iterations.

---

## 2. Configuration Schema Reference

Below is the exhaustive specification of all fields supported in `system_config.json`:

| Field Name | Type | Default Value | Allowed Values / Constraints | Section Status | Description |
|---|---|---|---|---|---|
| `websocket_host` | `string` | `"127.0.0.1"` | Valid IP address or hostname string | Reserved (Sec 10) | Bind address for the live WebSocket server broadcasting spatial state to Unreal Engine 5. |
| `websocket_port` | `integer` | `8765` | `1` to `65535` | Reserved (Sec 10) | Network port for the WebSocket server. |
| `database_path` | `string` | `"data/wisense.db"` | Non-empty file path string | Reserved (Sec 9) | Target path for SQLite database storing captured CSI frames, extracted features, and classification events. |
| `log_level` | `string` | `"INFO"` | `"DEBUG"`, `"INFO"`, `"WARNING"`, `"ERROR"`, `"CRITICAL"` | Active (Sec 3) | Minimum severity threshold for emitted logs (case-insensitive). |
| `log_format` | `string` | `"text"` | `"text"`, `"json"` | Active (Sec 3) | Output layout format for structured log emission. `"text"` produces human-readable console lines; `"json"` emits single-line JSON objects with standard fields and metadata. |
| `csi_source_mode` | `string` | `"synthetic"` | `"synthetic"`, `"hardware"` | Active (Sec 2) | Frame ingestion source mode. `"synthetic"` selects `SyntheticCSISource` (Section 1); `"hardware"` selects ESP32 serial ingestion (Section 15, raising `NotImplementedError` until implemented). |
| `synthetic_scenario_path` | `string \| null` | `"config/synthetic_scenarios/example.json"` | File path string; must not be null/empty when `csi_source_mode` is `"synthetic"` | Active (Sec 2) | Path to synthetic scenario JSON defining subcarriers, sample rate, duration, and motion events. |
| `serial_port` | `string \| null` | `null` | String (e.g. `"/dev/ttyUSB0"` or `"COM3"`) or `null` | Reserved (Sec 15) | Operating system serial port identifier for real ESP32 CSI receiver. |
| `serial_baud_rate` | `integer \| null` | `null` | Positive integer (e.g. `115200`, `921600`) or `null` | Reserved (Sec 15) | Baud rate for binary serial UART frame ingestion. |

---

## 3. Sample Configuration File

The default system configuration is stored at `backend_pipeline/config/system_config.json`:

```json
{
  "websocket_host": "127.0.0.1",
  "websocket_port": 8765,
  "database_path": "data/wisense.db",
  "log_level": "INFO",
  "log_format": "text",
  "csi_source_mode": "synthetic",
  "synthetic_scenario_path": "config/synthetic_scenarios/example.json",
  "serial_port": null,
  "serial_baud_rate": null
}
```

---

## 4. Structured Logging Configurations

### 4.1 Development Console Mode (`log_format: "text"`)
Outputs human-readable log records:
```
2026-09-14 14:45:30 [INFO] [wisense_backend.app] Application starting up (csi_source_mode: synthetic)
2026-09-14 14:45:30 [INFO] [wisense_backend.app] CSI record source opened successfully
2026-09-14 14:45:30 [INFO] [wisense_backend.app] Application shutdown complete. Total records processed: 300
```

### 4.2 Machine-Parseable Mode (`log_format: "json"`)
Outputs JSON lines suitable for ingestion into log aggregation tools:
```json
{"timestamp": "2026-09-14T09:15:30.123456+00:00", "level": "INFO", "logger": "wisense_backend.app", "message": "Application starting up (csi_source_mode: synthetic)", "csi_source_mode": "synthetic"}
{"timestamp": "2026-09-14T09:15:30.145678+00:00", "level": "INFO", "logger": "wisense_backend.app", "message": "Application shutdown complete. Total records processed: 300", "records_processed": 300}
```

---

## 5. Path Resolution Rules

When loading configuration and synthetic scenarios via `Application`:
1. **Absolute Paths**: Used directly.
2. **Current Directory**: Tested relative to the working directory (`cwd`).
3. **Backend Root Resolution**: Tested relative to `backend_pipeline/` root directory.
4. **Repository Root Resolution**: Tested relative to the repository workspace root.

This ensures seamless invocation whether launching via `python backend_pipeline/main.py` from repository root, `python main.py` within `backend_pipeline`, or running automated test suites in `pytest`.
