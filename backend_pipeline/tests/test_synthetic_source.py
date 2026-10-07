"""Unit tests for SyntheticCSISource and CSIRecordSource protocol."""

import json
import math
import time
from pathlib import Path

import numpy as np
import pytest

from wisense_backend.contracts.csi_source import CSIRecord, CSIRecordSource
from wisense_backend.ingestion.synthetic_source import (
    ScenarioConfig,
    ScenarioEvent,
    SyntheticCSISource,
)


class TestCSIRecordContract:
    """Validate data integrity and immutability of CSIRecord."""

    def test_record_creation_valid(self) -> None:
        amps = (10.0, 12.5, 14.2)
        epoch_ts = 1715000000.5
        rec = CSIRecord(
            timestamp=epoch_ts,
            subcarrier_count=3,
            amplitudes=amps,
            rssi=-42.0,
            channel=6,
            sequence_number=10,
        )
        assert rec.timestamp == epoch_ts
        assert rec.subcarrier_count == 3
        assert rec.amplitudes == amps
        assert rec.phases is None
        assert rec.rssi == -42.0
        assert rec.channel == 6
        assert rec.sequence_number == 10

    def test_record_frozen_immutability(self) -> None:
        rec = CSIRecord(
            timestamp=1715000000.0,
            subcarrier_count=2,
            amplitudes=(5.0, 6.0),
        )
        with pytest.raises(Exception):  # FrozenInstanceError
            rec.timestamp = 1715000001.0  # type: ignore

    def test_record_validation_mismatched_length(self) -> None:
        with pytest.raises(ValueError, match="amplitudes length"):
            CSIRecord(
                timestamp=1715000000.0,
                subcarrier_count=4,
                amplitudes=(1.0, 2.0, 3.0),  # Only 3
            )

    def test_record_validation_invalid_subcarrier_count(self) -> None:
        with pytest.raises(ValueError, match="subcarrier_count must be positive"):
            CSIRecord(
                timestamp=1715000000.0,
                subcarrier_count=0,
                amplitudes=(),
            )

    def test_record_validation_invalid_timestamp(self) -> None:
        with pytest.raises(ValueError, match="positive UTC Unix epoch timestamp"):
            CSIRecord(
                timestamp=0.0,
                subcarrier_count=2,
                amplitudes=(1.0, 2.0),
            )
        with pytest.raises(ValueError, match="positive UTC Unix epoch timestamp"):
            CSIRecord(
                timestamp=-10.0,
                subcarrier_count=2,
                amplitudes=(1.0, 2.0),
            )


class TestSyntheticSourceProtocol:
    """Ensure SyntheticCSISource conforms to CSIRecordSource protocol."""

    def test_implements_protocol(self) -> None:
        source = SyntheticCSISource()
        assert isinstance(source, CSIRecordSource)

    def test_context_manager_lifecycle(self) -> None:
        source = SyntheticCSISource()
        assert not source.is_open
        with source as s:
            assert s.is_open
            record = s.read_record()
            assert record is not None
        assert not source.is_open


class TestConfigValidation:
    """Validate scenario configuration constraints and error handling."""

    def test_valid_config_from_dict(self) -> None:
        data = {
            "scenario_name": "test_scn",
            "sample_rate_hz": 25.0,
            "subcarrier_count": 52,
            "zone_count": 3,
            "duration_seconds": 2.0,
            "events": [
                {
                    "start_time_s": 0.5,
                    "end_time_s": 1.5,
                    "zone_id": 2,
                    "motion_type": "walking",
                    "intensity": 1.2,
                }
            ],
        }
        cfg = ScenarioConfig.from_dict(data)
        assert cfg.scenario_name == "test_scn"
        assert cfg.sample_rate_hz == 25.0
        assert cfg.subcarrier_count == 52
        assert cfg.zone_count == 3
        assert cfg.duration_seconds == 2.0
        assert len(cfg.events) == 1
        assert cfg.events[0].zone_id == 2

    def test_invalid_sample_rate(self) -> None:
        with pytest.raises(ValueError, match="sample_rate_hz must be positive"):
            ScenarioConfig(sample_rate_hz=-10.0)

    def test_invalid_zone_id_out_of_bounds(self) -> None:
        with pytest.raises(ValueError, match="exceeds configured zone_count"):
            ScenarioConfig(
                zone_count=3,
                events=(ScenarioEvent(start_time_s=1.0, end_time_s=2.0, zone_id=4),),
            )

    def test_invalid_event_timing(self) -> None:
        with pytest.raises(ValueError, match="must be strictly greater than start_time_s"):
            ScenarioEvent(start_time_s=5.0, end_time_s=3.0, zone_id=1)

    def test_load_example_json_file(self) -> None:
        example_path = Path(__file__).parent.parent / "config" / "synthetic_scenarios" / "example.json"
        assert example_path.exists(), f"Missing example config at {example_path}"
        cfg = ScenarioConfig.from_json_file(example_path)
        assert cfg.scenario_name == "zone_2_motion_walkthrough"
        assert cfg.subcarrier_count == 64
        assert cfg.zone_count == 4
        assert cfg.sample_rate_hz == 20.0
        assert cfg.duration_seconds == 15.0
        assert len(cfg.events) == 1
        assert cfg.events[0].zone_id == 2


class TestGeneratorOutputAndRate:
    """Verify output dimensions, timing, and rates generated by SyntheticCSISource."""

    def test_exact_sample_count_and_dimensions(self) -> None:
        sample_rate = 25.0
        duration = 2.0
        subcarriers = 52
        fixed_epoch = 1700000000.0
        cfg = ScenarioConfig(
            sample_rate_hz=sample_rate,
            duration_seconds=duration,
            subcarrier_count=subcarriers,
            realtime=False,
            start_epoch_time=fixed_epoch,
        )

        with SyntheticCSISource(cfg) as source:
            records = list(source.records())

        expected_count = int(round(sample_rate * duration))  # 50
        assert len(records) == expected_count

        for i, rec in enumerate(records):
            assert rec.subcarrier_count == subcarriers
            assert len(rec.amplitudes) == subcarriers
            assert rec.sequence_number == i
            # Timestamp must be an epoch timestamp matching start_epoch_time + i * dt
            assert rec.timestamp >= 1_000_000_000.0
            expected_timestamp = fixed_epoch + (i * (1.0 / sample_rate))
            assert math.isclose(rec.timestamp, expected_timestamp, abs_tol=1e-5)

    def test_default_timestamp_is_current_epoch(self) -> None:
        """Verify that when start_epoch_time is None, timestamps use time.time()."""
        before_time = time.time()
        cfg = ScenarioConfig(duration_seconds=0.1, sample_rate_hz=10.0, realtime=False)
        with SyntheticCSISource(cfg) as source:
            rec = source.read_record()
        after_time = time.time()

        assert rec is not None
        assert rec.timestamp >= 1_000_000_000.0
        assert before_time <= rec.timestamp <= after_time + 0.1

    def test_realtime_pacing_rate(self) -> None:
        """Verify that realtime=True paces generation consistent with sample_rate_hz."""
        sample_rate = 20.0  # 50ms per frame
        duration = 0.25     # ~5 frames, ~250ms
        cfg = ScenarioConfig(
            sample_rate_hz=sample_rate,
            duration_seconds=duration,
            subcarrier_count=32,
            realtime=True,
        )

        start_wall = time.monotonic()
        with SyntheticCSISource(cfg) as source:
            records = list(source)
        elapsed_wall = time.monotonic() - start_wall

        assert len(records) == 5
        # Expected wall-clock duration is ~0.20 to 0.25s; allow reasonable tolerance
        assert elapsed_wall >= 0.18, f"Realtime generation finished too quickly: {elapsed_wall}s"
        assert elapsed_wall <= 0.45, f"Realtime generation finished too slowly: {elapsed_wall}s"


class TestPatternAndZoneInjection:
    """Verify injected motion matches configured zones and timing."""

    def test_quiet_vs_active_zone_variance(self) -> None:
        """Verify quiet intervals have low variance, and motion creates high variance in target zone."""
        cfg = ScenarioConfig(
            scenario_name="zone2_test",
            sample_rate_hz=20.0,
            duration_seconds=12.0,
            subcarrier_count=64,
            zone_count=4,
            noise_std=0.3,
            realtime=False,
            random_seed=123,
            events=(
                ScenarioEvent(
                    start_time_s=4.0,
                    end_time_s=8.0,
                    zone_id=2,
                    motion_type="walking",
                    intensity=1.0,
                ),
            ),
        )

        with SyntheticCSISource(cfg) as source:
            records = list(source)

        quiet_pre_amps: list[tuple[float, ...]] = []
        motion_amps: list[tuple[float, ...]] = []
        quiet_post_amps: list[tuple[float, ...]] = []

        start_epoch = records[0].timestamp
        for rec in records:
            rel_t = rec.timestamp - start_epoch
            if rel_t < 3.5:
                quiet_pre_amps.append(rec.amplitudes)
            elif 4.5 <= rel_t <= 7.5:
                motion_amps.append(rec.amplitudes)
            elif rel_t > 8.5:
                quiet_post_amps.append(rec.amplitudes)

        # Convert to numpy arrays: shape (N_samples, subcarriers)
        quiet_arr = np.array(quiet_pre_amps)
        motion_arr = np.array(motion_amps)
        post_arr = np.array(quiet_post_amps)

        # Variance across time for each subcarrier
        quiet_var = np.var(quiet_arr, axis=0)
        motion_var = np.var(motion_arr, axis=0)
        post_var = np.var(post_arr, axis=0)

        # During quiet periods, variance should be roughly noise_std^2 (~0.09)
        assert np.mean(quiet_var) < 0.25
        assert np.mean(post_var) < 0.25

        # During motion in Zone 2, motion variance should be significantly higher overall
        assert np.mean(motion_var) > 1.5 * np.mean(quiet_var)

        # Zone 2 subcarriers (centered around index ~24 in 64 subcarriers across 4 zones)
        # should have significantly higher variance than distant subcarriers (e.g., Zone 4)
        z2_center = int(((2 - 0.5) / 4) * 64)  # 24
        z4_center = int(((4 - 0.5) / 4) * 64)  # 56

        # Check local window around Zone 2 vs Zone 4 center
        z2_var = np.mean(motion_var[z2_center - 4 : z2_center + 5])
        z4_var = np.mean(motion_var[z4_center - 4 : z4_center + 5])

        assert z2_var > 2.0 * z4_var, (
            f"Zone 2 variance ({z2_var:.3f}) should strongly exceed "
            f"Zone 4 variance ({z4_var:.3f}) during Zone 2 motion."
        )

    def test_generic_n_zones(self) -> None:
        """Verify generator works generically for any N zones (e.g. 5 zones)."""
        cfg = ScenarioConfig(
            zone_count=5,
            subcarrier_count=80,
            sample_rate_hz=20.0,
            duration_seconds=6.0,
            realtime=False,
            events=(
                ScenarioEvent(start_time_s=1.0, end_time_s=3.0, zone_id=1),
                ScenarioEvent(start_time_s=3.5, end_time_s=5.5, zone_id=5),
            ),
        )

        with SyntheticCSISource(cfg) as source:
            records = list(source)

        assert len(records) == 120
        # Active zone 1 at t=2.0s
        ev1 = source.get_active_events_at(2.0)
        assert len(ev1) == 1 and ev1[0].zone_id == 1

        # Active zone 5 at t=4.0s
        ev5 = source.get_active_events_at(4.0)
        assert len(ev5) == 1 and ev5[0].zone_id == 5

        # Quiet at t=3.2s
        ev_quiet = source.get_active_events_at(3.2)
        assert len(ev_quiet) == 0
