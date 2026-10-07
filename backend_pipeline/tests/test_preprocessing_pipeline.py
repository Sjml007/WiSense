"""Integration and unit tests for PreprocessingPipeline composition and ordering."""

import json
import math
from pathlib import Path

import pytest

from wisense_backend.contracts.csi_source import CSIRecord
from wisense_backend.contracts.preprocessor import Preprocessor
from wisense_backend.ingestion.synthetic_source import (
    ScenarioConfig,
    SyntheticCSISource,
)
from wisense_backend.preprocessing.hampel_filter import HampelConfig, HampelFilter
from wisense_backend.preprocessing.moving_average import (
    MovingAverageConfig,
    MovingAverageFilter,
)
from wisense_backend.preprocessing.pipeline import (
    FilterStepConfig,
    PreprocessingConfig,
    PreprocessingPipeline,
)


def create_sample_record(
    timestamp: float,
    amplitudes: tuple[float, ...],
    seq: int = 1,
) -> CSIRecord:
    return CSIRecord(
        timestamp=timestamp,
        subcarrier_count=len(amplitudes),
        amplitudes=amplitudes,
        rssi=-42.0,
        channel=6,
        sequence_number=seq,
    )


class TestPreprocessingPipeline:
    """Validate pipeline chaining, ordering, toggle bypass, and end-to-end ingestion."""

    def test_implements_preprocessor_protocol(self) -> None:
        """Verify PreprocessingPipeline conforms to Preprocessor protocol (Composite pattern)."""
        pipe = PreprocessingPipeline(filters=[])
        assert isinstance(pipe, Preprocessor)

    def test_from_config_ordering_and_construction(self) -> None:
        """Verify pipeline builds constituent filters in the exact order configured."""
        config_data = {
            "filters": [
                {
                    "type": "hampel",
                    "enabled": True,
                    "parameters": {"window_size": 7, "threshold_multiplier": 3.0},
                },
                {
                    "type": "moving_average",
                    "enabled": True,
                    "parameters": {"window_size": 5},
                },
            ]
        }
        cfg = PreprocessingConfig.from_dict(config_data)
        pipeline = PreprocessingPipeline.from_config(cfg)

        assert len(pipeline.filters) == 2
        assert isinstance(pipeline.filters[0], HampelFilter)
        assert isinstance(pipeline.filters[1], MovingAverageFilter)
        assert pipeline.filters[0].config.window_size == 7
        assert pipeline.filters[1].config.window_size == 5

    def test_disabled_filter_is_genuinely_skipped(self) -> None:
        """Verify setting enabled=False completely excludes the filter from the pipeline."""
        config_data = {
            "filters": [
                {
                    "type": "hampel",
                    "enabled": False,  # DISABLED
                    "parameters": {"window_size": 7},
                },
                {
                    "type": "moving_average",
                    "enabled": True,  # ENABLED
                    "parameters": {"window_size": 5},
                },
            ]
        }
        cfg = PreprocessingConfig.from_dict(config_data)
        pipeline = PreprocessingPipeline.from_config(cfg)

        # Only moving_average should be present
        assert len(pipeline.filters) == 1
        assert isinstance(pipeline.filters[0], MovingAverageFilter)

        # Test that an outlier spike is NOT suppressed when Hampel is disabled
        records = [create_sample_record(1700000000.0 + i, (30.0,)) for i in range(10)]
        # Inject spike at frame 5
        records[5] = create_sample_record(1700000005.0, (300.0,), seq=5)

        processed = [pipeline.process(r) for r in records]
        # Without Hampel, the spike remains prominent (only smoothed by moving average)
        assert processed[5].amplitudes[0] > 70.0  # Hampel would have suppressed to ~30.0

    def test_filter_ordering_hampel_then_moving_average(self) -> None:
        """Verify Hampel suppresses the outlier first before Moving Average smooths the sequence."""
        cfg = PreprocessingConfig(
            filters=(
                FilterStepConfig(
                    filter_type="hampel",
                    parameters={"window_size": 7, "threshold_multiplier": 3.0},
                ),
                FilterStepConfig(
                    filter_type="moving_average",
                    parameters={"window_size": 5},
                ),
            )
        )
        pipeline = PreprocessingPipeline.from_config(cfg)

        records = [create_sample_record(1700000000.0 + i, (30.0,)) for i in range(15)]
        records[8] = create_sample_record(1700000008.0, (300.0,), seq=8)

        processed = [pipeline.process(r) for r in records]

        # Because Hampel caught the spike before moving average, the spike never leaked
        # into moving average, so the amplitude remains close to baseline 30.0 throughout
        for rec in processed:
            assert math.isclose(rec.amplitudes[0], 30.0, abs_tol=0.5)

    def test_reverse_filter_ordering(self) -> None:
        """Verify filters execute in reverse order when configured in reverse."""
        cfg = PreprocessingConfig(
            filters=(
                FilterStepConfig(
                    filter_type="moving_average",
                    parameters={"window_size": 5},
                ),
                FilterStepConfig(
                    filter_type="hampel",
                    parameters={"window_size": 7, "threshold_multiplier": 3.0},
                ),
            )
        )
        pipeline = PreprocessingPipeline.from_config(cfg)
        assert isinstance(pipeline.filters[0], MovingAverageFilter)
        assert isinstance(pipeline.filters[1], HampelFilter)

    def test_sample_preprocessing_config_json(self) -> None:
        """Verify the project's config/preprocessing_config.json loads and instantiates cleanly."""
        config_path = (
            Path(__file__).resolve().parent.parent / "config" / "preprocessing_config.json"
        )
        assert config_path.is_file(), f"Sample config not found at {config_path}"

        pipeline = PreprocessingPipeline.from_json_file(config_path)
        assert len(pipeline.filters) == 2
        assert isinstance(pipeline.filters[0], HampelFilter)
        assert isinstance(pipeline.filters[1], MovingAverageFilter)

    def test_synthetic_csi_source_end_to_end_integration(self) -> None:
        """Integration test: Run Section 1 SyntheticCSISource through PreprocessingPipeline.

        Confirms output record count, subcarrier dimensionality (64), and metadata integrity.
        """
        scenario_path = (
            Path(__file__).resolve().parent.parent
            / "config"
            / "synthetic_scenarios"
            / "example.json"
        )
        scenario_cfg = ScenarioConfig.from_json_file(scenario_path)

        config_path = (
            Path(__file__).resolve().parent.parent / "config" / "preprocessing_config.json"
        )
        pipeline = PreprocessingPipeline.from_json_file(config_path)

        with SyntheticCSISource(scenario_cfg) as source:
            raw_records = list(source)

        assert len(raw_records) == 300

        # Process all frames through pipeline
        processed_records = list(pipeline.process_stream(raw_records))

        # 1. Output record count must exactly match input
        assert len(processed_records) == 300

        # 2. Frame structure and subcarrier count must match input exactly (64 subcarriers)
        for raw, cleaned in zip(raw_records, processed_records):
            assert cleaned.timestamp == raw.timestamp
            assert cleaned.subcarrier_count == 64
            assert len(cleaned.amplitudes) == 64
            assert cleaned.channel == raw.channel
            assert cleaned.rssi == raw.rssi
            assert cleaned.sequence_number == raw.sequence_number
            # Check values are finite numbers
            assert all(math.isfinite(a) for a in cleaned.amplitudes)
