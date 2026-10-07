"""Unit tests for MovingAverageFilter smoothing."""

import math
from pathlib import Path

import numpy as np
import pytest

from wisense_backend.contracts.csi_source import CSIRecord
from wisense_backend.contracts.preprocessor import Preprocessor
from wisense_backend.preprocessing.moving_average import (
    MovingAverageConfig,
    MovingAverageFilter,
)


def create_sample_record(
    timestamp: float,
    amplitudes: tuple[float, ...],
    phases: tuple[float, ...] | None = None,
    seq: int = 1,
) -> CSIRecord:
    """Helper to construct a valid CSIRecord for tests."""
    return CSIRecord(
        timestamp=timestamp,
        subcarrier_count=len(amplitudes),
        amplitudes=amplitudes,
        phases=phases,
        rssi=-45.0,
        channel=6,
        sequence_number=seq,
    )


class TestMovingAverageFilter:
    """Validate Moving Average filter behavior, variance reduction, and startup."""

    def test_implements_preprocessor_protocol(self) -> None:
        """Verify MovingAverageFilter conforms to Preprocessor protocol."""
        flt = MovingAverageFilter()
        assert isinstance(flt, Preprocessor)

    def test_config_validation(self) -> None:
        """Verify invalid window sizes raise ValueError."""
        with pytest.raises(ValueError, match="window_size must be >= 1"):
            MovingAverageConfig(window_size=0)
        with pytest.raises(ValueError, match="window_size must be >= 1"):
            MovingAverageConfig(window_size=-5)

    def test_variance_reduction_on_noisy_signal(self) -> None:
        """Verify filter meaningfully reduces variance on stationary noisy signal while preserving mean."""
        window_size = 7
        cfg = MovingAverageConfig(window_size=window_size)
        flt = MovingAverageFilter(cfg)

        # Generate 200 samples of stationary Gaussian noise around mean = 30.0
        np.random.seed(42)
        target_mean = 30.0
        input_noise_std = 2.0
        num_samples = 200

        noise = np.random.normal(loc=target_mean, scale=input_noise_std, size=num_samples)

        filtered_amps: list[float] = []
        for i, val in enumerate(noise):
            rec = create_sample_record(
                timestamp=1700000000.0 + i * 0.05,
                amplitudes=(float(val),),
                seq=i,
            )
            cleaned = flt.process(rec)
            filtered_amps.append(cleaned.amplitudes[0])

        # Exclude initial startup frames to evaluate steady state
        steady_input = noise[window_size:]
        steady_output = np.array(filtered_amps[window_size:])

        input_var = float(np.var(steady_input))
        output_var = float(np.var(steady_output))

        input_mean = float(np.mean(steady_input))
        output_mean = float(np.mean(steady_output))

        # 1. Output variance must be meaningfully lower than input variance
        # Theoretical reduction for window W is approximately 1/W
        assert output_var < input_var * 0.4, (
            f"Expected variance reduction: input_var={input_var:.4f}, output_var={output_var:.4f}"
        )

        # 2. Filter must track the identical mean within tight tolerance
        assert math.isclose(output_mean, input_mean, abs_tol=0.1), (
            f"Mean shifted unexpectedly: input_mean={input_mean:.4f}, output_mean={output_mean:.4f}"
        )

    def test_window_size_1_is_identity(self) -> None:
        """Verify window_size=1 acts as an exact identity pass-through."""
        cfg = MovingAverageConfig(window_size=1)
        flt = MovingAverageFilter(cfg)

        test_values = [(10.0, 20.0), (15.5, 25.5), (30.0, 40.0)]
        for i, val in enumerate(test_values):
            rec = create_sample_record(1700000000.0 + i, val)
            cleaned = flt.process(rec)
            assert cleaned.amplitudes == val

    def test_startup_behavior(self) -> None:
        """Verify early frames (< window_size) compute running mean with zero drops."""
        cfg = MovingAverageConfig(window_size=4)
        flt = MovingAverageFilter(cfg)

        records = [
            create_sample_record(1.0, (10.0,)),
            create_sample_record(2.0, (20.0,)),
            create_sample_record(3.0, (30.0,)),
            create_sample_record(4.0, (40.0,)),
        ]

        # Frame 1: mean([10]) = 10.0
        out1 = flt.process(records[0])
        assert math.isclose(out1.amplitudes[0], 10.0)

        # Frame 2: mean([10, 20]) = 15.0
        out2 = flt.process(records[1])
        assert math.isclose(out2.amplitudes[0], 15.0)

        # Frame 3: mean([10, 20, 30]) = 20.0
        out3 = flt.process(records[2])
        assert math.isclose(out3.amplitudes[0], 20.0)

        # Frame 4: mean([10, 20, 30, 40]) = 25.0
        out4 = flt.process(records[3])
        assert math.isclose(out4.amplitudes[0], 25.0)

    def test_metadata_preservation(self) -> None:
        """Verify all non-signal metadata fields are preserved."""
        flt = MovingAverageFilter()
        rec = CSIRecord(
            timestamp=1715000000.5,
            subcarrier_count=2,
            amplitudes=(12.0, 14.0),
            phases=(0.5, -0.5),
            rssi=-50.0,
            channel=1,
            sequence_number=999,
        )
        cleaned = flt.process(rec)

        assert cleaned.timestamp == rec.timestamp
        assert cleaned.subcarrier_count == rec.subcarrier_count
        assert cleaned.phases == rec.phases
        assert cleaned.rssi == rec.rssi
        assert cleaned.channel == rec.channel
        assert cleaned.sequence_number == rec.sequence_number

    def test_reset_clears_buffer(self) -> None:
        """Verify reset() clears internal rolling window."""
        flt = MovingAverageFilter(MovingAverageConfig(window_size=5))
        flt.process(create_sample_record(1.0, (10.0,)))
        assert len(flt._amplitude_history) == 1

        flt.reset()
        assert len(flt._amplitude_history) == 0
