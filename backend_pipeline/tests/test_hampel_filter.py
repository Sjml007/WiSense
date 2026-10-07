"""Unit tests for HampelFilter outlier suppression."""

import math
from pathlib import Path

import numpy as np
import pytest

from wisense_backend.contracts.csi_source import CSIRecord
from wisense_backend.contracts.preprocessor import Preprocessor
from wisense_backend.preprocessing.hampel_filter import HampelConfig, HampelFilter


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


class TestHampelFilter:
    """Validate Hampel filter behavior, outlier suppression, and edge cases."""

    def test_implements_preprocessor_protocol(self) -> None:
        """Verify HampelFilter conforms to the Preprocessor runtime protocol."""
        flt = HampelFilter()
        assert isinstance(flt, Preprocessor)

    def test_config_validation(self) -> None:
        """Verify invalid configuration parameters raise ValueError."""
        with pytest.raises(ValueError, match="window_size must be >= 3"):
            HampelConfig(window_size=2)
        with pytest.raises(ValueError, match="threshold_multiplier must be > 0.0"):
            HampelConfig(threshold_multiplier=0.0)
        with pytest.raises(ValueError, match="threshold_multiplier must be > 0.0"):
            HampelConfig(threshold_multiplier=-1.5)
        with pytest.raises(ValueError, match="min_mad must be non-negative"):
            HampelConfig(min_mad=-0.1)

    def test_outlier_suppression_10x_spike(self) -> None:
        """Test Hampel suppresses an injected 10x spike on one subcarrier without distorting clean samples."""
        cfg = HampelConfig(window_size=7, threshold_multiplier=3.0)
        flt = HampelFilter(cfg)

        num_subcarriers = 4
        num_frames = 15
        baseline = 30.0
        spike_index = 8
        spike_sc = 2

        raw_records: list[CSIRecord] = []
        for i in range(num_frames):
            amps = [baseline] * num_subcarriers
            # Inject a 10x outlier spike at frame 8 on subcarrier 2
            if i == spike_index:
                amps[spike_sc] = baseline * 10.0  # 300.0 vs baseline 30.0

            raw_records.append(
                create_sample_record(
                    timestamp=1700000000.0 + i * 0.05,
                    amplitudes=tuple(amps),
                    seq=i,
                )
            )

        cleaned_records = [flt.process(r) for r in raw_records]

        # 1. Verify the spike at frame 8, subcarrier 2 was suppressed back to baseline
        spike_record = cleaned_records[spike_index]
        assert math.isclose(spike_record.amplitudes[spike_sc], baseline, abs_tol=0.5), (
            f"Expected spike {baseline * 10.0} to be suppressed to ~{baseline}, "
            f"got {spike_record.amplitudes[spike_sc]}"
        )

        # 2. Verify all other subcarriers at frame 8 remained undistorted
        for sc in range(num_subcarriers):
            if sc != spike_sc:
                assert math.isclose(spike_record.amplitudes[sc], baseline, abs_tol=1e-5)

        # 3. Verify clean frames surrounding the spike (e.g. frame 7 and 9) were not distorted
        for i in range(num_frames):
            if i != spike_index:
                for sc in range(num_subcarriers):
                    assert math.isclose(
                        cleaned_records[i].amplitudes[sc], baseline, abs_tol=1e-5
                    ), f"Clean sample at frame {i}, subcarrier {sc} was distorted!"

    def test_clean_signal_no_false_positives(self) -> None:
        """Verify that a clean signal with normal thermal noise does not trigger false outlier suppression."""
        cfg = HampelConfig(window_size=7, threshold_multiplier=3.0)
        flt = HampelFilter(cfg)

        np.random.seed(123)
        # 50 samples with small thermal noise (std=0.15) around 30.0
        noise = np.random.normal(0, 0.15, 50)
        amps_series = 30.0 + noise

        unaltered_count = 0
        for i, amp in enumerate(amps_series):
            rec = create_sample_record(1700000000.0 + i * 0.05, (float(amp),), seq=i)
            cleaned = flt.process(rec)
            if math.isclose(cleaned.amplitudes[0], amp, abs_tol=1e-5):
                unaltered_count += 1

        # Virtually all normal noisy samples (>= 96%) must pass through completely unaltered
        assert unaltered_count >= 48

    def test_zero_mad_flat_signal(self) -> None:
        """Verify that constant signals (where MAD == 0) do not trigger false outlier replacements."""
        cfg = HampelConfig(window_size=5, threshold_multiplier=3.0, min_mad=1e-6)
        flt = HampelFilter(cfg)

        for i in range(10):
            rec = create_sample_record(
                timestamp=1700000000.0 + i * 0.05,
                amplitudes=(25.0, 25.0),
                seq=i,
            )
            cleaned = flt.process(rec)
            assert cleaned.amplitudes == (25.0, 25.0)

    def test_record_metadata_preservation(self) -> None:
        """Verify non-signal attributes are strictly preserved on returned CSIRecord."""
        flt = HampelFilter()
        phases = (0.1, 0.2, 0.3)
        rec = CSIRecord(
            timestamp=1715000000.123,
            subcarrier_count=3,
            amplitudes=(10.0, 20.0, 30.0),
            phases=phases,
            rssi=-48.5,
            channel=11,
            sequence_number=101,
        )

        cleaned = flt.process(rec)

        assert cleaned.timestamp == rec.timestamp
        assert cleaned.subcarrier_count == rec.subcarrier_count
        assert cleaned.phases == rec.phases  # phase filtering disabled by default
        assert cleaned.rssi == rec.rssi
        assert cleaned.channel == rec.channel
        assert cleaned.sequence_number == rec.sequence_number

    def test_reset_clears_buffer(self) -> None:
        """Verify reset() empties history buffers."""
        flt = HampelFilter(HampelConfig(window_size=5))
        rec = create_sample_record(timestamp=1700000000.0, amplitudes=(10.0, 20.0))

        flt.process(rec)
        flt.process(rec)
        assert len(flt._amplitude_history) == 2

        flt.reset()
        assert len(flt._amplitude_history) == 0

    def test_phase_filtering_when_enabled(self) -> None:
        """Verify phase spikes are filtered when filter_phases=True."""
        cfg = HampelConfig(window_size=5, threshold_multiplier=3.0, filter_phases=True)
        flt = HampelFilter(cfg)

        records: list[CSIRecord] = []
        for i in range(8):
            phase_val = 0.5
            if i == 5:
                phase_val = 50.0  # extreme phase spike
            rec = create_sample_record(
                timestamp=1700000000.0 + i * 0.05,
                amplitudes=(30.0,),
                phases=(phase_val,),
                seq=i,
            )
            records.append(flt.process(rec))

        assert records[5].phases is not None
        assert math.isclose(records[5].phases[0], 0.5, abs_tol=0.1)
