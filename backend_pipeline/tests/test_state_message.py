"""Unit tests for the StateMessage contract and wire protocol."""

import json
import math
from dataclasses import FrozenInstanceError

import pytest
from wisense_backend.contracts.state_message import StateMessage


class TestStateMessageCreation:
    """Tests for StateMessage instantiation and validation rules."""

    def test_valid_active_motion_message(self) -> None:
        """Verify instantiation of a valid active motion message with zone classification."""
        msg = StateMessage(
            schema_version=1,
            timestamp=1700000000.123,
            zone_id=2,
            is_motion=True,
            presence=True,
            separation_margin=0.045678,
            calibration_version=3,
        )
        assert msg.schema_version == 1
        assert msg.timestamp == 1700000000.123
        assert msg.zone_id == 2
        assert msg.is_motion is True
        assert msg.presence is True
        assert msg.separation_margin == 0.045678
        assert msg.calibration_version == 3

    def test_valid_quiet_state_message(self) -> None:
        """Verify instantiation of a quiet state (no motion, no presence, no zone)."""
        msg = StateMessage(
            schema_version=1,
            timestamp=1700000001.0,
            zone_id=None,
            is_motion=False,
            presence=False,
            separation_margin=None,
            calibration_version=1,
        )
        assert msg.is_motion is False
        assert msg.presence is False
        assert msg.zone_id is None
        assert msg.separation_margin is None

    def test_valid_presence_hold_state_message(self) -> None:
        """Verify motion=False with presence=True (motion ceased, presence timeout active)."""
        msg = StateMessage(
            schema_version=1,
            timestamp=1700000002.0,
            zone_id=None,
            is_motion=False,
            presence=True,
            separation_margin=None,
            calibration_version=1,
        )
        assert msg.is_motion is False
        assert msg.presence is True
        assert msg.zone_id is None

    def test_valid_motion_with_unclassified_zone(self) -> None:
        """Verify motion=True with unknown zone (zone_id=None, separation_margin=None)."""
        msg = StateMessage(
            schema_version=1,
            timestamp=1700000003.0,
            zone_id=None,
            is_motion=True,
            presence=True,
            separation_margin=None,
            calibration_version=1,
        )
        assert msg.is_motion is True
        assert msg.zone_id is None
        assert msg.separation_margin is None

    def test_immutability(self) -> None:
        """Verify StateMessage is frozen and cannot be mutated."""
        msg = StateMessage(
            schema_version=1,
            timestamp=1700000000.0,
            zone_id=1,
            is_motion=True,
            presence=True,
            separation_margin=0.1,
            calibration_version=1,
        )
        with pytest.raises(FrozenInstanceError):
            msg.zone_id = 2  # type: ignore[misc]


class TestStateMessageInvariants:
    """Tests for invariant violations and motion gating enforcement."""

    def test_schema_version_must_be_positive(self) -> None:
        """schema_version must be >= 1."""
        with pytest.raises(ValueError, match="schema_version must be >= 1"):
            StateMessage(
                schema_version=0,
                timestamp=1700000000.0,
                zone_id=None,
                is_motion=False,
                presence=False,
                separation_margin=None,
            )

    def test_timestamp_must_be_positive(self) -> None:
        """timestamp must be > 0.0."""
        with pytest.raises(ValueError, match="timestamp must be positive"):
            StateMessage(
                schema_version=1,
                timestamp=0.0,
                zone_id=None,
                is_motion=False,
                presence=False,
                separation_margin=None,
            )

    def test_is_motion_must_be_bool(self) -> None:
        """is_motion must be a bool, not int or truthy object."""
        with pytest.raises(ValueError, match="is_motion must be a bool"):
            StateMessage(
                schema_version=1,
                timestamp=1700000000.0,
                zone_id=None,
                is_motion=1,  # type: ignore[arg-type]
                presence=False,
                separation_margin=None,
            )

    def test_presence_must_be_bool(self) -> None:
        """presence must be a bool, not int or truthy object."""
        with pytest.raises(ValueError, match="presence must be a bool"):
            StateMessage(
                schema_version=1,
                timestamp=1700000000.0,
                zone_id=None,
                is_motion=False,
                presence=0,  # type: ignore[arg-type]
                separation_margin=None,
            )

    def test_motion_gating_rejects_zone_when_not_motion(self) -> None:
        """Audit #10 / Section 8 rule: zone_id must be None when is_motion is False."""
        with pytest.raises(
            ValueError, match="zone_id must be None when is_motion is False"
        ):
            StateMessage(
                schema_version=1,
                timestamp=1700000000.0,
                zone_id=1,
                is_motion=False,
                presence=True,
                separation_margin=None,
            )

    def test_motion_gating_rejects_margin_when_not_motion(self) -> None:
        """separation_margin must be None when is_motion is False."""
        with pytest.raises(
            ValueError, match="separation_margin must be None when is_motion is False"
        ):
            StateMessage(
                schema_version=1,
                timestamp=1700000000.0,
                zone_id=None,
                is_motion=False,
                presence=False,
                separation_margin=0.05,
            )

    def test_zone_id_type_and_range(self) -> None:
        """zone_id must be an integer >= 1."""
        with pytest.raises(TypeError, match="zone_id must be an int or None"):
            StateMessage(
                schema_version=1,
                timestamp=1700000000.0,
                zone_id="1",  # type: ignore[arg-type]
                is_motion=True,
                presence=True,
                separation_margin=None,
            )

        with pytest.raises(ValueError, match="zone_id must be >= 1"):
            StateMessage(
                schema_version=1,
                timestamp=1700000000.0,
                zone_id=0,
                is_motion=True,
                presence=True,
                separation_margin=None,
            )

    def test_separation_margin_validations(self) -> None:
        """separation_margin must be a finite non-negative float."""
        with pytest.raises(
            ValueError, match="separation_margin must be a finite non-negative number"
        ):
            StateMessage(
                schema_version=1,
                timestamp=1700000000.0,
                zone_id=1,
                is_motion=True,
                presence=True,
                separation_margin=-0.01,
            )

        with pytest.raises(
            ValueError, match="separation_margin must be a finite non-negative number"
        ):
            StateMessage(
                schema_version=1,
                timestamp=1700000000.0,
                zone_id=1,
                is_motion=True,
                presence=True,
                separation_margin=float("nan"),
            )

    def test_calibration_version_validation(self) -> None:
        """calibration_version must be >= 1 if provided."""
        with pytest.raises(ValueError, match="calibration_version must be >= 1"):
            StateMessage(
                schema_version=1,
                timestamp=1700000000.0,
                zone_id=None,
                is_motion=False,
                presence=False,
                separation_margin=None,
                calibration_version=0,
            )


class TestStateMessageSerialization:
    """Tests for JSON serialization, deserialization, and schema dictionary conversion."""

    def test_to_dict_and_from_dict_roundtrip(self) -> None:
        """Verify to_dict and from_dict produce identical message content."""
        msg = StateMessage(
            schema_version=1,
            timestamp=1700000000.123456,
            zone_id=3,
            is_motion=True,
            presence=True,
            separation_margin=0.12345678,
            calibration_version=2,
        )
        d = msg.to_dict()
        assert d["schema_version"] == 1
        assert d["timestamp"] == 1700000000.123456
        assert d["zone_id"] == 3
        assert d["is_motion"] is True
        assert d["presence"] is True
        assert d["separation_margin"] == 0.123457  # rounded to 6 decimal places
        assert d["calibration_version"] == 2

        restored = StateMessage.from_dict(d)
        assert restored.schema_version == msg.schema_version
        assert restored.timestamp == msg.timestamp
        assert restored.zone_id == msg.zone_id
        assert restored.is_motion == msg.is_motion
        assert restored.presence == msg.presence
        assert restored.separation_margin == 0.123457
        assert restored.calibration_version == msg.calibration_version

    def test_to_json_and_from_json_roundtrip(self) -> None:
        """Verify to_json and from_json produce valid JSON and faithful reconstruction."""
        msg = StateMessage(
            schema_version=1,
            timestamp=1700000000.5,
            zone_id=None,
            is_motion=False,
            presence=True,
            separation_margin=None,
            calibration_version=1,
        )
        json_str = msg.to_json()
        parsed_raw = json.loads(json_str)
        assert parsed_raw["zone_id"] is None
        assert parsed_raw["is_motion"] is False
        assert parsed_raw["presence"] is True
        assert parsed_raw["separation_margin"] is None

        restored = StateMessage.from_json(json_str)
        assert restored == msg

    def test_strict_naming_check_no_confidence(self) -> None:
        """Verify 'confidence' never appears in field names, dictionary keys, or wire json."""
        msg = StateMessage(
            schema_version=1,
            timestamp=1700000000.0,
            zone_id=1,
            is_motion=True,
            presence=True,
            separation_margin=0.25,
            calibration_version=1,
        )
        d = msg.to_dict()
        json_str = msg.to_json()

        for key in d.keys():
            assert "confidence" not in key.lower(), f"Forbidden word found in key: {key}"

        assert "confidence" not in json_str.lower(), "Forbidden word found in JSON payload"

        for attr in dir(msg):
            assert "confidence" not in attr.lower(), f"Forbidden word in attribute: {attr}"
