"""Unit tests for SlidingWindowBuffer."""

import pytest

from wisense_backend.contracts.csi_source import CSIRecord
from wisense_backend.features.sliding_window import SlidingWindowBuffer, WindowConfig


def make_record(ts: float, amps: tuple[float, ...] = (10.0, 20.0), seq: int = 1) -> CSIRecord:
    return CSIRecord(
        timestamp=ts,
        subcarrier_count=len(amps),
        amplitudes=amps,
        rssi=-45.0,
        channel=6,
        sequence_number=seq,
    )


class TestSlidingWindowBuffer:
    """Validate window buffering, step advancement, and partial-window policy."""

    def test_config_validation(self) -> None:
        """Verify invalid window parameters raise ValueError."""
        with pytest.raises(ValueError, match="window_size_samples must be >= 2"):
            WindowConfig(window_size_samples=1)
        with pytest.raises(ValueError, match="window_size_samples must be >= 2"):
            WindowConfig(window_size_samples=0)
        with pytest.raises(ValueError, match="window_step_samples must be >= 1"):
            WindowConfig(window_step_samples=0)

    def test_continuous_sliding_window_step_1(self) -> None:
        """Verify W=5, S=1 emits nothing for first 4 frames, then emits every frame."""
        buf = SlidingWindowBuffer(WindowConfig(window_size_samples=5, window_step_samples=1, emit_partial=False))

        # Records 1..4: should return None
        for i in range(1, 5):
            win = buf.add_record(make_record(float(i), seq=i))
            assert win is None
            assert not buf.is_full
            assert buf.sample_count == i

        # Record 5: first complete window
        win5 = buf.add_record(make_record(5.0, seq=5))
        assert win5 is not None
        assert buf.is_full
        assert len(win5) == 5
        assert [r.sequence_number for r in win5] == [1, 2, 3, 4, 5]

        # Record 6: step=1, emits next sliding window (records 2..6)
        win6 = buf.add_record(make_record(6.0, seq=6))
        assert win6 is not None
        assert len(win6) == 5
        assert [r.sequence_number for r in win6] == [2, 3, 4, 5, 6]

        # Record 7: step=1, emits next sliding window (records 3..7)
        win7 = buf.add_record(make_record(7.0, seq=7))
        assert win7 is not None
        assert len(win7) == 5
        assert [r.sequence_number for r in win7] == [3, 4, 5, 6, 7]

    def test_strided_sliding_window_step_2(self) -> None:
        """Verify W=4, S=2 emits window at record 4, skips record 5, emits at record 6."""
        buf = SlidingWindowBuffer(WindowConfig(window_size_samples=4, window_step_samples=2, emit_partial=False))

        records = [make_record(float(i), seq=i) for i in range(1, 8)]

        # Rec 1..3: None
        assert buf.add_record(records[0]) is None
        assert buf.add_record(records[1]) is None
        assert buf.add_record(records[2]) is None

        # Rec 4: full window (1, 2, 3, 4)
        win4 = buf.add_record(records[3])
        assert win4 is not None
        assert [r.sequence_number for r in win4] == [1, 2, 3, 4]

        # Rec 5: step=2, so record 5 returns None
        win5 = buf.add_record(records[4])
        assert win5 is None

        # Rec 6: step=2 reached, emits window (3, 4, 5, 6)
        win6 = buf.add_record(records[5])
        assert win6 is not None
        assert [r.sequence_number for r in win6] == [3, 4, 5, 6]

    def test_partial_window_policy_true(self) -> None:
        """Verify emit_partial=True emits early windows from record 1."""
        buf = SlidingWindowBuffer(WindowConfig(window_size_samples=5, window_step_samples=1, emit_partial=True))

        win1 = buf.add_record(make_record(1.0, seq=1))
        assert win1 is not None
        assert len(win1) == 1

        win2 = buf.add_record(make_record(2.0, seq=2))
        assert win2 is not None
        assert len(win2) == 2

    def test_subcarrier_count_mismatch_raises_error(self) -> None:
        """Verify adding a record with mismatched subcarrier count raises ValueError."""
        buf = SlidingWindowBuffer(WindowConfig(window_size_samples=4))
        buf.add_record(make_record(1.0, amps=(10.0, 20.0)))

        with pytest.raises(ValueError, match="Subcarrier count mismatch"):
            buf.add_record(make_record(2.0, amps=(10.0, 20.0, 30.0)))

    def test_reset(self) -> None:
        """Verify reset clears buffer and counters."""
        buf = SlidingWindowBuffer(WindowConfig(window_size_samples=3))
        buf.add_record(make_record(1.0))
        buf.add_record(make_record(2.0))
        assert buf.sample_count == 2

        buf.reset()
        assert buf.sample_count == 0
        assert not buf.is_full
        assert buf.get_current_window() == ()
