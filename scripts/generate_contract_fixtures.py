#!/usr/bin/env python3
"""Generates contract test fixtures for StateMessage wire protocol verification.

Writes:
  contracts/fixtures/state_message/*.json
  contracts/fixtures/state_message/manifest.json

Valid fixtures are created using StateMessage.to_json().
Invalid fixtures are hand-written edge cases.
expected_python is dynamically determined by evaluating StateMessage.from_json().
"""

from __future__ import annotations

import json
from pathlib import Path
import sys

# Ensure backend_pipeline/src is on python path
REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND_SRC = REPO_ROOT / "backend_pipeline" / "src"
if str(BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(BACKEND_SRC))

from wisense_backend.contracts.state_message import StateMessage


def generate_fixtures() -> dict[str, any]:
    output_dir = REPO_ROOT / "contracts" / "fixtures" / "state_message"
    output_dir.mkdir(parents=True, exist_ok=True)

    # 1. Valid fixtures (built via StateMessage, never hand-typed)
    valid_specs: dict[str, tuple[StateMessage, str]] = {
        "active_zone": (
            StateMessage(
                schema_version=1,
                timestamp=1700000000.123456,
                zone_id=2,
                is_motion=True,
                presence=True,
                separation_margin=0.034567,
                calibration_version=1,
            ),
            "Valid state with active motion and confident zone assignment",
        ),
        "ambiguous_motion": (
            StateMessage(
                schema_version=1,
                timestamp=1700000001.234567,
                zone_id=None,
                is_motion=True,
                presence=True,
                separation_margin=None,
                calibration_version=1,
            ),
            "Valid active motion where zone is ambiguous (zone_id and margin null)",
        ),
        "presence_hold": (
            StateMessage(
                schema_version=1,
                timestamp=1700000002.345678,
                zone_id=None,
                is_motion=False,
                presence=True,
                separation_margin=None,
                calibration_version=1,
            ),
            "Valid quiet period during presence timeout hold (motion false, presence true)",
        ),
        "quiet": (
            StateMessage(
                schema_version=1,
                timestamp=1700000003.456789,
                zone_id=None,
                is_motion=False,
                presence=False,
                separation_margin=None,
                calibration_version=1,
            ),
            "Valid completely idle state (motion false, presence false)",
        ),
    }

    # 2. Invalid fixtures (hand-written edge cases & malformations)
    invalid_specs: dict[str, tuple[str, str]] = {
        "legacy_occupied_format": (
            '{"zone": 2, "occupied": true}',
            "Legacy prototype format lacking schema_version, timestamp, and distinct motion flags",
        ),
        "missing_schema_version": (
            '{"timestamp": 1700000000.123456, "zone_id": 2, "is_motion": true, "presence": true, "separation_margin": 0.034567, "calibration_version": 1}',
            "Missing required schema_version field",
        ),
        "unsupported_schema_version": (
            '{"schema_version": 2, "timestamp": 1700000000.123456, "zone_id": 2, "is_motion": true, "presence": true, "separation_margin": 0.034567, "calibration_version": 1}',
            "Unsupported protocol schema_version (expected 1, got 2)",
        ),
        "zone_as_string": (
            '{"schema_version": 1, "timestamp": 1700000000.123456, "zone_id": "2", "is_motion": true, "presence": true, "separation_margin": 0.034567, "calibration_version": 1}',
            "Zone ID encoded as string rather than integer",
        ),
        "zone_zero": (
            '{"schema_version": 1, "timestamp": 1700000000.123456, "zone_id": 0, "is_motion": true, "presence": true, "separation_margin": 0.034567, "calibration_version": 1}',
            "Invalid non-positive zone ID (zones are 1-indexed, zone_id >= 1)",
        ),
        "zone_fractional": (
            '{"schema_version": 1, "timestamp": 1700000000.123456, "zone_id": 2.5, "is_motion": true, "presence": true, "separation_margin": 0.034567, "calibration_version": 1}',
            "Invalid fractional zone ID (must be an integer)",
        ),
        "motion_false_with_zone": (
            '{"schema_version": 1, "timestamp": 1700000000.123456, "zone_id": 2, "is_motion": false, "presence": true, "separation_margin": null, "calibration_version": 1}',
            "Motion gating violation: zone_id must be null when is_motion is false",
        ),
        "malformed_json": (
            '{"schema_version": 1, "timestamp": 1700000000.123456, "zone_id": 2, broken_syntax',
            "Malformed JSON syntax",
        ),
    }

    manifest_entries = []

    # Process valid fixtures
    for name, (msg, desc) in valid_specs.items():
        filename = f"{name}.json"
        raw_json = msg.to_json()
        (output_dir / filename).write_text(raw_json, encoding="utf-8")

        # Test against Python
        try:
            StateMessage.from_json(raw_json)
            expected_python = True
        except Exception:
            expected_python = False

        manifest_entries.append(
            {
                "name": name,
                "file": filename,
                "expected_unreal": True,
                "expected_python": expected_python,
                "description": desc,
            }
        )

    # Process invalid fixtures
    for name, (raw_json, desc) in invalid_specs.items():
        filename = f"{name}.json"
        (output_dir / filename).write_text(raw_json, encoding="utf-8")

        # Test against Python
        try:
            StateMessage.from_json(raw_json)
            expected_python = True
        except Exception:
            expected_python = False

        manifest_entries.append(
            {
                "name": name,
                "file": filename,
                "expected_unreal": False,
                "expected_python": expected_python,
                "description": desc,
            }
        )

    manifest_data = {
        "version": 1,
        "description": "WiSense StateMessage v1 contract fixtures and expected parse results across runtimes",
        "asymmetry_notes": {
            "missing_schema_version": "Python from_dict defaults schema_version to 1 if absent; Unreal strictly requires schema_version == 1",
            "unsupported_schema_version": "Python allows schema_version >= 1 without checking == 1; Unreal strictly enforces schema_version == 1",
            "zone_as_string": "Python int(data['zone_id']) coerces string digits to int; Unreal rejects non-numeric types",
            "zone_fractional": "Python int(data['zone_id']) truncates float 2.5 to int 2; Unreal rejects non-integer numbers",
        },
        "fixtures": manifest_entries,
    }

    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest_data, indent=2), encoding="utf-8")
    print(f"Generated {len(manifest_entries)} fixtures in {output_dir}")
    print(f"Wrote manifest: {manifest_path}")
    return manifest_data


if __name__ == "__main__":
    generate_fixtures()
