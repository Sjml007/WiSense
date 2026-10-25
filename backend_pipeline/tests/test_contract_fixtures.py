"""Table-driven test suite verifying StateMessage wire protocol fixtures against manifest."""

from __future__ import annotations

import json
from pathlib import Path
import pytest

from wisense_backend.contracts.state_message import StateMessage

# Locate manifest file
FIXTURES_DIR = (
    Path(__file__).resolve().parent.parent.parent
    / "contracts"
    / "fixtures"
    / "state_message"
)
MANIFEST_PATH = FIXTURES_DIR / "manifest.json"


def load_manifest_cases():
    assert MANIFEST_PATH.exists(), f"Manifest file missing at {MANIFEST_PATH}"
    with open(MANIFEST_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data.get("fixtures", [])


FIXTURE_CASES = load_manifest_cases()


@pytest.mark.parametrize(
    "case",
    FIXTURE_CASES,
    ids=[case["name"] for case in FIXTURE_CASES],
)
def test_state_message_fixture_contract(case: dict):
    """Verify that Python parser behavior matches expected_python in the manifest."""
    fixture_file = FIXTURES_DIR / case["file"]
    assert fixture_file.exists(), f"Fixture file missing: {fixture_file}"

    raw_content = fixture_file.read_text(encoding="utf-8")
    expected_python = case["expected_python"]

    if expected_python:
        # Must parse without exception
        msg = StateMessage.from_json(raw_content)
        assert isinstance(msg, StateMessage)

        # Invariants must hold
        if not msg.is_motion:
            assert msg.zone_id is None
            assert msg.separation_margin is None
        if msg.zone_id is not None:
            assert msg.zone_id >= 1
    else:
        # Must raise an exception during JSON decoding or StateMessage validation
        with pytest.raises((Exception,)):
            StateMessage.from_json(raw_content)
