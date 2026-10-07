"""Configurable Synthetic CSI Data Generator.

This module implements `SyntheticCSISource`, conforming to the `CSIRecordSource`
protocol. It generates realistic CSI amplitude records across a configurable number
of subcarriers, with injected motion and zone signatures driven by scenario configurations.

Key Design Highlights:
- Generic for any zone count (N zones) and subcarrier count (OFDM 52, 64, 128, etc.).
- Fully config-driven: sample rate, duration, zones, and injected motion intervals.
- Plausible, illustrative multipath model (not a validated real-world signature):
  zones activate distinct, localized subcarrier frequency bands and Doppler-like
  dynamic perturbations as a testing heuristic.
- Dual-mode timing:
    * `realtime=True`: paced via clock sleep matching `sample_rate_hz` for live pipelines.
    * `realtime=False`: zero-delay generation for rapid unit testing and model training.
- Zero synthetic/ground-truth leakage into `CSIRecord`, ensuring downstream modules
  cannot differentiate between synthetic and real hardware inputs.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, List, Optional, Sequence, Tuple

import numpy as np

if __package__ is None or __package__ == "":
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
    from wisense_backend.contracts.csi_source import CSIRecord, CSIRecordSource
else:
    from wisense_backend.contracts.csi_source import CSIRecord, CSIRecordSource


@dataclass(frozen=True)
class ScenarioEvent:
    """Represents a scheduled human motion interval in a specific zone.

    Attributes:
        start_time_s: Event start time in relative seconds from scenario start.
        end_time_s: Event end time in relative seconds from scenario start.
        zone_id: 1-indexed identifier of the active room zone (1 <= zone_id <= zone_count).
        motion_type: Descriptive motion type ("walking", "subtle", "waving", etc.).
        intensity: Scaling factor for motion perturbation (default 1.0, higher = larger swing).
    """

    start_time_s: float
    end_time_s: float
    zone_id: int
    motion_type: str = "walking"
    intensity: float = 1.0

    def __post_init__(self) -> None:
        if self.start_time_s < 0:
            raise ValueError(f"start_time_s must be non-negative, got {self.start_time_s}")
        if self.end_time_s <= self.start_time_s:
            raise ValueError(
                f"end_time_s ({self.end_time_s}) must be strictly greater than "
                f"start_time_s ({self.start_time_s})"
            )
        if self.zone_id < 1:
            raise ValueError(f"zone_id must be >= 1 (1-indexed), got {self.zone_id}")
        if self.intensity < 0.0:
            raise ValueError(f"intensity must be non-negative, got {self.intensity}")


@dataclass(frozen=True)
class ScenarioConfig:
    """Complete specification for a synthetic CSI generation run.

    Attributes:
        scenario_name: Unique identifier for the scenario.
        description: Human-readable description of the scenario.
        sample_rate_hz: Target sampling frequency in Hertz.
        subcarrier_count: Total number of OFDM subcarriers per frame.
        zone_count: Number of monitored physical zones in the room.
        duration_seconds: Total scenario duration in seconds (0.0 = infinite stream).
        realtime: If True, paces execution using wall-clock sleep; if False, generates immediately.
        baseline_amplitude: Nominal mean CSI amplitude in undisturbed state.
        noise_std: Standard deviation of static Gaussian thermal noise.
        baseline_rssi: Nominal baseline RSSI in dBm.
        channel: WiFi RF channel number.
        random_seed: Seed for reproducible pseudorandom sequence generation.
        start_epoch_time: Optional UTC Unix epoch timestamp (seconds) for scenario start.
            If None, defaults to time.time() at stream open.
        events: List of scheduled motion events over time.
    """

    scenario_name: str = "default_scenario"
    description: str = ""
    sample_rate_hz: float = 20.0
    subcarrier_count: int = 64
    zone_count: int = 4
    duration_seconds: float = 10.0
    realtime: bool = False
    baseline_amplitude: float = 30.0
    noise_std: float = 0.3
    baseline_rssi: float = -45.0
    channel: int = 6
    random_seed: Optional[int] = 42
    start_epoch_time: Optional[float] = None
    events: tuple[ScenarioEvent, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if self.sample_rate_hz <= 0.0:
            raise ValueError(f"sample_rate_hz must be positive, got {self.sample_rate_hz}")
        if self.subcarrier_count <= 0:
            raise ValueError(f"subcarrier_count must be positive, got {self.subcarrier_count}")
        if self.zone_count <= 0:
            raise ValueError(f"zone_count must be positive, got {self.zone_count}")
        if self.duration_seconds < 0.0:
            raise ValueError(f"duration_seconds must be non-negative, got {self.duration_seconds}")
        if self.noise_std < 0.0:
            raise ValueError(f"noise_std must be non-negative, got {self.noise_std}")
        if self.start_epoch_time is not None and self.start_epoch_time <= 0.0:
            raise ValueError(f"start_epoch_time must be positive, got {self.start_epoch_time}")

        # Validate that all events reference valid zones within zone_count
        for event in self.events:
            if event.zone_id > self.zone_count:
                raise ValueError(
                    f"Event zone_id {event.zone_id} exceeds configured zone_count {self.zone_count}"
                )

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ScenarioConfig:
        """Parse a ScenarioConfig from a dictionary."""
        events_raw = data.get("events", [])
        events = tuple(
            ScenarioEvent(
                start_time_s=float(e["start_time_s"]),
                end_time_s=float(e["end_time_s"]),
                zone_id=int(e["zone_id"]),
                motion_type=str(e.get("motion_type", "walking")),
                intensity=float(e.get("intensity", 1.0)),
            )
            for e in events_raw
        )

        return cls(
            scenario_name=str(data.get("scenario_name", "unnamed_scenario")),
            description=str(data.get("description", "")),
            sample_rate_hz=float(data.get("sample_rate_hz", 20.0)),
            subcarrier_count=int(data.get("subcarrier_count", 64)),
            zone_count=int(data.get("zone_count", 4)),
            duration_seconds=float(data.get("duration_seconds", 10.0)),
            realtime=bool(data.get("realtime", False)),
            baseline_amplitude=float(data.get("baseline_amplitude", 30.0)),
            noise_std=float(data.get("noise_std", 0.3)),
            baseline_rssi=float(data.get("baseline_rssi", -45.0)),
            channel=int(data.get("channel", 6)),
            random_seed=data.get("random_seed"),
            start_epoch_time=(
                float(data["start_epoch_time"])
                if data.get("start_epoch_time") is not None
                else None
            ),
            events=events,
        )

    @classmethod
    def from_json_file(cls, file_path: str | Path) -> ScenarioConfig:
        """Load scenario configuration from a JSON file."""
        path = Path(file_path)
        if not path.is_file():
            raise FileNotFoundError(f"Scenario configuration file not found: {path}")
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return cls.from_dict(data)


class SyntheticCSISource(CSIRecordSource):
    """Generates synthetic CSI frames simulating indoor WiFi multipath propagation.

    Conforms to `CSIRecordSource` protocol.
    """

    def __init__(
        self,
        config: Optional[ScenarioConfig] = None,
        *,
        config_path: Optional[str | Path] = None,
        realtime_override: Optional[bool] = None,
    ) -> None:
        """Initialize the synthetic source.

        Args:
            config: An explicit `ScenarioConfig` instance.
            config_path: Path to a JSON configuration file (used if `config` is None).
            realtime_override: If provided, overrides the `realtime` flag in the config.
        """
        if config is not None:
            self._config = config
        elif config_path is not None:
            self._config = ScenarioConfig.from_json_file(config_path)
        else:
            self._config = ScenarioConfig()

        if realtime_override is not None:
            self._realtime = realtime_override
        else:
            self._realtime = self._config.realtime

        self._is_open: bool = False
        self._seq_num: int = 0
        self._current_sample: int = 0
        self._total_samples: int = (
            int(round(self._config.duration_seconds * self._config.sample_rate_hz))
            if self._config.duration_seconds > 0.0
            else -1
        )
        self._dt: float = 1.0 / self._config.sample_rate_hz

        # Reproducible random state
        self._rng = np.random.default_rng(self._config.random_seed)

        # Precompute static spatial profile across subcarriers
        # In a real room, subcarriers have slight static multipath notches and peaks
        sc_indices = np.arange(self._config.subcarrier_count, dtype=np.float64)
        static_multipath_ripple = 2.5 * np.sin(
            2 * np.pi * sc_indices / (self._config.subcarrier_count / 2.0)
        ) + 1.0 * np.cos(4 * np.pi * sc_indices / self._config.subcarrier_count)
        self._static_profile = self._config.baseline_amplitude + static_multipath_ripple

        # Precompute zone spatial sensitivity masks:
        # Each zone z has a spectral center c_z and spread sigma_z across subcarriers.
        # This guarantees generic N-zone support with distinct, physically plausible signatures.
        self._zone_masks: dict[int, np.ndarray] = {}
        for z in range(1, self._config.zone_count + 1):
            center = ((z - 0.5) / self._config.zone_count) * self._config.subcarrier_count
            spread = max(2.5, self._config.subcarrier_count / (2.0 * self._config.zone_count))
            mask = np.exp(-0.5 * ((sc_indices - center) / spread) ** 2)
            self._zone_masks[z] = mask

        # Internal timing and epoch tracking
        self._stream_start_wall_clock: Optional[float] = None
        self._stream_start_epoch: float = 0.0

    @property
    def config(self) -> ScenarioConfig:
        """Access the active scenario configuration."""
        return self._config

    @property
    def is_open(self) -> bool:
        """Return whether the stream is open."""
        return self._is_open

    def open(self) -> None:
        """Initialize the synthetic stream."""
        if self._is_open:
            return
        self._is_open = True
        self._seq_num = 0
        self._current_sample = 0
        self._rng = np.random.default_rng(self._config.random_seed)
        self._stream_start_wall_clock = time.monotonic()
        self._stream_start_epoch = (
            self._config.start_epoch_time
            if self._config.start_epoch_time is not None
            else time.time()
        )

    def close(self) -> None:
        """Close the stream and reset state."""
        self._is_open = False
        self._stream_start_wall_clock = None

    def get_active_events_at(self, relative_time_s: float) -> list[ScenarioEvent]:
        """Return all scenario events active at the given relative elapsed time (s).

        This helper provides ground-truth inspection for testing and validation
        without modifying or polluting the public `CSIRecord`.
        """
        active: list[ScenarioEvent] = []
        for event in self._config.events:
            if event.start_time_s <= relative_time_s < event.end_time_s:
                active.append(event)
        return active

    def read_record(self, timeout: Optional[float] = None) -> Optional[CSIRecord]:
        """Read the next synthetic CSI frame.

        Args:
            timeout: Unused in synthetic generation unless already closed/finished.

        Returns:
            The next `CSIRecord`, or `None` if the scenario duration has elapsed
            or the source is closed.
        """
        if not self._is_open:
            return None

        if self._total_samples >= 0 and self._current_sample >= self._total_samples:
            return None

        rel_time_s = self._current_sample * self._dt

        # Pacing for real-time simulation if enabled
        if self._realtime and self._stream_start_wall_clock is not None:
            target_wall_time = self._stream_start_wall_clock + rel_time_s
            now = time.monotonic()
            sleep_duration = target_wall_time - now
            if sleep_duration > 0.001:
                time.sleep(sleep_duration)

        # 1. Start with static baseline multipath profile
        amplitudes = np.copy(self._static_profile)

        # 2. Add static Gaussian thermal noise
        if self._config.noise_std > 0.0:
            amplitudes += self._rng.normal(
                0.0, self._config.noise_std, size=self._config.subcarrier_count
            )

        # 3. Apply active motion events for the current timestamp
        active_events = self.get_active_events_at(rel_time_s)
        rssi_drop = 0.0

        for event in active_events:
            mask = self._zone_masks.get(event.zone_id)
            if mask is None:
                continue

            # Motion frequency characteristic (Doppler-like oscillation ~ 2-5 Hz)
            motion_freq = 3.5 if event.motion_type == "walking" else 1.8
            # Dynamic oscillatory perturbation
            oscillation = 4.5 * np.sin(2.0 * np.pi * motion_freq * rel_time_s)
            # High-frequency motion-induced turbulence/scattering noise
            turbulence = self._rng.normal(0.0, 1.8, size=self._config.subcarrier_count)

            motion_delta = event.intensity * mask * (oscillation + turbulence)
            amplitudes += motion_delta

            # Shadowing effect on RSSI during human presence
            rssi_drop += 1.5 * event.intensity

        # Ensure amplitudes are strictly positive
        amplitudes = np.clip(amplitudes, 0.1, None)

        record_timestamp = self._stream_start_epoch + rel_time_s
        record_rssi = self._config.baseline_rssi - rssi_drop

        record = CSIRecord(
            timestamp=record_timestamp,
            subcarrier_count=self._config.subcarrier_count,
            amplitudes=tuple(float(round(v, 4)) for v in amplitudes),
            phases=None,  # Phase features are experimental and disabled by default
            rssi=float(round(record_rssi, 2)),
            channel=self._config.channel,
            sequence_number=self._seq_num,
        )

        self._seq_num += 1
        self._current_sample += 1

        return record

    def records(self) -> Iterator[CSIRecord]:
        """Yield all scenario records sequentially until completion."""
        if not self._is_open:
            self.open()

        while self._is_open:
            record = self.read_record()
            if record is None:
                break
            yield record

    def records_with_ground_truth(self) -> Iterator[tuple[CSIRecord, list[ScenarioEvent]]]:
        """Yield records paired with ground-truth active events for test harnesses."""
        if not self._is_open:
            self.open()

        while self._is_open:
            rel_time_s = self._current_sample * self._dt
            events = self.get_active_events_at(rel_time_s)
            record = self.read_record()
            if record is None:
                break
            yield record, events

    def __iter__(self) -> Iterator[CSIRecord]:
        return self.records()

    def __enter__(self) -> SyntheticCSISource:
        self.open()
        return self

    def __exit__(self, exc_type: object, exc_val: object, exc_tb: object) -> None:
        self.close()


def run_cli() -> None:
    """Command-line runner and standalone test harness for SyntheticCSISource."""
    parser = argparse.ArgumentParser(
        description="WiSense-Twin Synthetic CSI Data Generator Standalone Harness"
    )
    parser.add_argument(
        "--config",
        "-c",
        type=str,
        default=None,
        help="Path to scenario JSON configuration file",
    )
    parser.add_argument(
        "--realtime",
        "-r",
        action="store_true",
        help="Enforce real-time wall-clock pacing matching sample_rate_hz",
    )
    parser.add_argument(
        "--duration",
        "-d",
        type=float,
        default=None,
        help="Override scenario duration in seconds",
    )
    parser.add_argument(
        "--limit",
        "-l",
        type=int,
        default=None,
        help="Limit number of frames to emit",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Print detailed subcarrier summary per frame",
    )
    args = parser.parse_args()

    config = None
    if args.config:
        config = ScenarioConfig.from_json_file(args.config)
        if args.duration is not None:
            config = ScenarioConfig.from_dict({**config.__dict__, "duration_seconds": args.duration})
    else:
        # Built-in sample scenario
        sample_events = (
            ScenarioEvent(start_time_s=2.0, end_time_s=5.0, zone_id=2, motion_type="walking"),
            ScenarioEvent(start_time_s=7.0, end_time_s=10.0, zone_id=1, motion_type="subtle"),
        )
        duration = args.duration if args.duration is not None else 12.0
        config = ScenarioConfig(
            scenario_name="builtin_demo",
            description="Built-in CLI demonstration scenario",
            duration_seconds=duration,
            sample_rate_hz=20.0,
            subcarrier_count=64,
            zone_count=4,
            events=sample_events,
        )

    realtime_flag = args.realtime or config.realtime

    print("=================================================================")
    print("        WiSense-Twin: Synthetic CSI Data Generator Harness       ")
    print("=================================================================")
    print(f" Scenario Name    : {config.scenario_name}")
    print(f" Subcarrier Count : {config.subcarrier_count}")
    print(f" Zone Count       : {config.zone_count}")
    print(f" Sample Rate      : {config.sample_rate_hz:.1f} Hz")
    print(f" Duration         : {config.duration_seconds:.1f} s")
    print(f" Realtime Paced   : {realtime_flag}")
    print(f" Events Scheduled : {len(config.events)}")
    for i, ev in enumerate(config.events, 1):
        print(
            f"   [{i}] t={ev.start_time_s:.1f}s -> {ev.end_time_s:.1f}s | "
            f"Zone {ev.zone_id} | Type: {ev.motion_type} | Intensity: {ev.intensity:.1f}"
        )
    print("-----------------------------------------------------------------")
    print("Starting generator stream...\n")

    start_wall = time.monotonic()
    total_frames = 0

    with SyntheticCSISource(config, realtime_override=realtime_flag) as source:
        for record, active_events in source.records_with_ground_truth():
            total_frames += 1
            amps = np.array(record.amplitudes)
            mean_amp = np.mean(amps)
            std_amp = np.std(amps)
            zones_str = (
                ", ".join(f"Zone {e.zone_id} ({e.motion_type})" for e in active_events)
                if active_events
                else "QUIET (No motion)"
            )

            if args.verbose or total_frames % int(config.sample_rate_hz) == 0 or total_frames == 1:
                rel_s = (total_frames - 1) / config.sample_rate_hz
                print(
                    f"Frame #{record.sequence_number:05d} | "
                    f"epoch={record.timestamp:.3f} (+{rel_s:5.2f}s) | "
                    f"Mean Amp: {mean_amp:5.2f} | "
                    f"Std: {std_amp:4.2f} | "
                    f"RSSI: {record.rssi:5.1f} dBm | "
                    f"Status: {zones_str}"
                )

            if args.limit and total_frames >= args.limit:
                print(f"\nReached frame limit ({args.limit}). Stopping.")
                break

    elapsed_wall = time.monotonic() - start_wall
    effective_fps = total_frames / elapsed_wall if elapsed_wall > 0 else 0.0

    print("-----------------------------------------------------------------")
    print("Stream completed successfully.")
    print(f" Total frames generated : {total_frames}")
    print(f" Wall-clock elapsed     : {elapsed_wall:.3f} s")
    print(f" Effective throughput   : {effective_fps:.1f} frames/sec")
    print("=================================================================")


if __name__ == "__main__":
    run_cli()
