"""Unit tests for LegacyZoneMessage contract and LegacyZoneAdapter translator."""

import json
import logging
from typing import Optional

import pytest

from wisense_backend.communication.legacy_zone_adapter import LegacyZoneAdapter
from wisense_backend.contracts.legacy_zone_message import LegacyZoneMessage
from wisense_backend.contracts.state_message import StateMessage


class TestLegacyZoneMessage:
    """Validate wire format, type enforcement, and serialization of LegacyZoneMessage."""

    def test_exact_wire_bytes_compact_json(self) -> None:
        """Verify exact compact JSON with no whitespace and real JSON types."""
        msg_true = LegacyZoneMessage(zone=2, occupied=True)
        json_true = msg_true.to_json()
        assert json_true == '{"zone":2,"occupied":true}'

        msg_false = LegacyZoneMessage(zone=4, occupied=False)
        json_false = msg_false.to_json()
        assert json_false == '{"zone":4,"occupied":false}'

    def test_payload_contains_exactly_two_keys(self) -> None:
        """Verify the JSON representation has exactly keys 'zone' and 'occupied'."""
        msg = LegacyZoneMessage(zone=1, occupied=True)
        parsed = json.loads(msg.to_json())
        assert set(parsed.keys()) == {"zone", "occupied"}
        assert len(parsed) == 2

    def test_wire_types_are_int_and_bool_not_strings_or_numbers(self) -> None:
        """Verify wire types are strictly int and bool, never numeric bools or string representations."""
        msg = LegacyZoneMessage(zone=3, occupied=False)
        parsed = json.loads(msg.to_json())
        assert type(parsed["zone"]) is int
        assert type(parsed["occupied"]) is bool
        assert not isinstance(parsed["zone"], bool)
        assert parsed["zone"] == 3
        assert parsed["occupied"] is False

    def test_zone_less_than_one_rejected(self) -> None:
        """Reject zone < 1."""
        with pytest.raises(ValueError, match="zone must be >= 1"):
            LegacyZoneMessage(zone=0, occupied=True)

        with pytest.raises(ValueError, match="zone must be >= 1"):
            LegacyZoneMessage(zone=-5, occupied=False)

    def test_type_enforcement(self) -> None:
        """Reject non-integer zone and non-bool occupied values."""
        with pytest.raises(TypeError, match="zone must be an integer"):
            LegacyZoneMessage(zone="2", occupied=True)  # type: ignore[arg-type]

        with pytest.raises(TypeError, match="zone must be an integer"):
            LegacyZoneMessage(zone=True, occupied=True)  # type: ignore[arg-type]

        with pytest.raises(TypeError, match="zone must be an integer"):
            LegacyZoneMessage(zone=2.5, occupied=True)  # type: ignore[arg-type]

        with pytest.raises(TypeError, match="occupied must be a boolean"):
            LegacyZoneMessage(zone=2, occupied=1)  # type: ignore[arg-type]

        with pytest.raises(TypeError, match="occupied must be a boolean"):
            LegacyZoneMessage(zone=2, occupied="true")  # type: ignore[arg-type]

        with pytest.raises(TypeError, match="occupied must be a boolean"):
            LegacyZoneMessage(zone=2, occupied=None)  # type: ignore[arg-type]

    def test_from_json_and_to_dict(self) -> None:
        """Verify parsing from JSON wire string and dictionary export."""
        raw = '{"zone":3,"occupied":true}'
        msg = LegacyZoneMessage.from_json(raw)
        assert msg.zone == 3
        assert msg.occupied is True
        assert msg.to_dict() == {"zone": 3, "occupied": True}

        # Reject payload with extra keys
        with pytest.raises(ValueError, match="exactly keys 'zone' and 'occupied'"):
            LegacyZoneMessage.from_json('{"zone":1,"occupied":true,"extra":42}')

        # Reject non-object payload
        with pytest.raises(ValueError, match="JSON object"):
            LegacyZoneMessage.from_json('[1, true]')


class FakeClock:
    """Deterministic clock controllable by tests."""

    def __init__(self, start_time: float = 100.0) -> None:
        self.current_time = start_time

    def __call__(self) -> float:
        return self.current_time

    def advance(self, delta_seconds: float) -> None:
        self.current_time += delta_seconds


def make_state(
    zone_id: Optional[int],
    is_motion: bool,
    presence: bool,
    timestamp: float = 1700000000.0,
) -> StateMessage:
    """Helper creating valid StateMessage instances for adapter tests."""
    return StateMessage(
        schema_version=1,
        timestamp=timestamp,
        zone_id=zone_id,
        is_motion=is_motion,
        presence=presence,
        separation_margin=0.05 if zone_id is not None and is_motion else None,
        calibration_version=1,
    )


class TestLegacyZoneAdapter:
    """Table-driven unit tests for LegacyZoneAdapter translation logic."""

    def test_constructor_validation(self) -> None:
        """Verify zone_count validation on initialization."""
        adapter = LegacyZoneAdapter(zone_count=4)
        assert adapter.zone_count == 4
        assert adapter.occupied_zone is None

        with pytest.raises(ValueError, match="zone_count must be >= 1"):
            LegacyZoneAdapter(zone_count=0)

        with pytest.raises(ValueError, match="zone_count must be >= 1"):
            LegacyZoneAdapter(zone_count=-1)

        with pytest.raises(TypeError, match="zone_count must be an integer"):
            LegacyZoneAdapter(zone_count="4")  # type: ignore[arg-type]

        with pytest.raises(TypeError, match="zone_count must be an integer"):
            LegacyZoneAdapter(zone_count=True)  # type: ignore[arg-type]

    def test_table_driven_mapping_full_lifecycle(self) -> None:
        """Verify each row of the translation behavior specification table.

        Table rows tested:
        1. is_motion=True, zone_id=k in 1..zone_count -> emits {k, true} (or old false then new true)
        2. is_motion=True, zone_id=None -> hold current zone, emit nothing
        3. is_motion=False, presence=True -> hold current zone, emit nothing
        4. presence=False -> emit {zone, false} and clear
        5. zone_id outside 1..zone_count -> ignore, throttled warning, emit nothing
        6. repeat of current state -> emit nothing (no duplicates)
        """
        adapter = LegacyZoneAdapter(zone_count=4)
        assert adapter.occupied_zone is None

        # --- Row 1: Initial motion in Zone 2 ---
        out1 = adapter.process(make_state(zone_id=2, is_motion=True, presence=True))
        assert out1 == [LegacyZoneMessage(zone=2, occupied=True)]
        assert adapter.occupied_zone == 2

        # --- Row 6: Repeat of current state -> emit nothing ---
        out2 = adapter.process(make_state(zone_id=2, is_motion=True, presence=True))
        assert out2 == []
        assert adapter.occupied_zone == 2

        # --- Row 2: Ambiguous motion (zone_id=None) -> hold current zone ---
        out3 = adapter.process(make_state(zone_id=None, is_motion=True, presence=True))
        assert out3 == []
        assert adapter.occupied_zone == 2

        # --- Row 3: Motion ceases, presence decay hold -> hold current zone ---
        out4 = adapter.process(make_state(zone_id=None, is_motion=False, presence=True))
        assert out4 == []
        assert adapter.occupied_zone == 2

        # --- Row 1 (Switch): Motion detected in Zone 4 -> emit old false BEFORE new true ---
        out5 = adapter.process(make_state(zone_id=4, is_motion=True, presence=True))
        assert out5 == [
            LegacyZoneMessage(zone=2, occupied=False),
            LegacyZoneMessage(zone=4, occupied=True),
        ]
        assert adapter.occupied_zone == 4

        # --- Row 5: Zone outside range (zone_id=5 when zone_count=4) -> ignore, emit nothing ---
        out6 = adapter.process(make_state(zone_id=5, is_motion=True, presence=True))
        assert out6 == []
        assert adapter.occupied_zone == 4

        # --- Row 4: Presence expires -> emit {zone, false} and clear ---
        out7 = adapter.process(make_state(zone_id=None, is_motion=False, presence=False))
        assert out7 == [LegacyZoneMessage(zone=4, occupied=False)]
        assert adapter.occupied_zone is None

        # --- Row 4 Repeated: Idle baseline -> emit nothing ---
        out8 = adapter.process(make_state(zone_id=None, is_motion=False, presence=False))
        assert out8 == []
        assert adapter.occupied_zone is None

    def test_switch_order_old_zone_false_before_new_zone_true(self) -> None:
        """Explicitly verify that a zone switch emits the previous zone's false before the new zone's true."""
        adapter = LegacyZoneAdapter(zone_count=4)
        adapter.process(make_state(zone_id=1, is_motion=True, presence=True))

        messages = adapter.process(make_state(zone_id=3, is_motion=True, presence=True))
        assert len(messages) == 2
        assert messages[0] == LegacyZoneMessage(zone=1, occupied=False)
        assert messages[1] == LegacyZoneMessage(zone=3, occupied=True)

    def test_snapshot_contents_and_arbitrary_zone_counts(self) -> None:
        """Verify snapshot() produces complete all-false or single-true state for N zones."""
        # 3 zones, idle
        adapter3 = LegacyZoneAdapter(zone_count=3)
        snap_idle = adapter3.snapshot()
        assert len(snap_idle) == 3
        assert snap_idle == [
            LegacyZoneMessage(zone=1, occupied=False),
            LegacyZoneMessage(zone=2, occupied=False),
            LegacyZoneMessage(zone=3, occupied=False),
        ]

        # 3 zones, zone 2 active
        adapter3.process(make_state(zone_id=2, is_motion=True, presence=True))
        snap_active = adapter3.snapshot()
        assert snap_active == [
            LegacyZoneMessage(zone=1, occupied=False),
            LegacyZoneMessage(zone=2, occupied=True),
            LegacyZoneMessage(zone=3, occupied=False),
        ]

        # 5 zones (verifying zone count is never hardcoded to 4)
        adapter5 = LegacyZoneAdapter(zone_count=5)
        adapter5.process(make_state(zone_id=5, is_motion=True, presence=True))
        snap5 = adapter5.snapshot()
        assert len(snap5) == 5
        assert snap5[4] == LegacyZoneMessage(zone=5, occupied=True)
        assert all(msg.occupied is False for msg in snap5[:4])

    def test_reset(self) -> None:
        """Verify reset() clears internal state and subsequent snapshot is all-false."""
        adapter = LegacyZoneAdapter(zone_count=4)
        adapter.process(make_state(zone_id=2, is_motion=True, presence=True))
        assert adapter.occupied_zone == 2

        adapter.reset()
        assert adapter.occupied_zone is None
        assert adapter.snapshot() == [
            LegacyZoneMessage(zone=1, occupied=False),
            LegacyZoneMessage(zone=2, occupied=False),
            LegacyZoneMessage(zone=3, occupied=False),
            LegacyZoneMessage(zone=4, occupied=False),
        ]

        # After reset, a repeat of zone 2 is now treated as a new activation
        out = adapter.process(make_state(zone_id=2, is_motion=True, presence=True))
        assert out == [LegacyZoneMessage(zone=2, occupied=True)]

    def test_out_of_range_warning_is_throttled(self, caplog: pytest.LogCaptureFixture) -> None:
        """Verify out-of-range zone logs a warning with time throttling."""
        clock = FakeClock(start_time=100.0)
        adapter = LegacyZoneAdapter(zone_count=4, clock=clock, warning_throttle_seconds=5.0)

        with caplog.at_level(logging.WARNING):
            # First out-of-range event: must log warning
            out1 = adapter.process(make_state(zone_id=9, is_motion=True, presence=True))
            assert out1 == []
            assert "outside configured range [1, 4]" in caplog.text

            caplog.clear()

            # Second event at t+2s (within 5s throttle): must emit nothing and NOT log
            clock.advance(2.0)
            out2 = adapter.process(make_state(zone_id=9, is_motion=True, presence=True))
            assert out2 == []
            assert caplog.text == ""

            # Third event at t+5.1s (past 5s throttle): must log warning again
            clock.advance(3.1)
            out3 = adapter.process(make_state(zone_id=9, is_motion=True, presence=True))
            assert out3 == []
            assert "outside configured range [1, 4]" in caplog.text
