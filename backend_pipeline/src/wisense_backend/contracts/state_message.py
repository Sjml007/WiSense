"""WebSocket wire state message contract for WiSense-Twin.

Defines `StateMessage`, the versioned JSON payload broadcast to connected clients
(Unreal Engine 5 digital twin, web dashboards, diagnostic tools).

AUDIT COMPLIANCE & WIRE CONTRACT SPECIFICATION:
------------------------------------------------
This replaces the original SRS proposed JSON payload, fixing critical audit gaps:
1. `schema_version`: Explicit versioning (starting at 1) enables clients (UE5) to detect
   protocol upgrades rather than silently misparsing fields.
2. Separate `is_motion` and `presence` Booleans: Replaces the ambiguous `"status": "occupied"`
   string with honest, distinct flags for instantaneous motion vs. timeout-derived presence.
3. Diagnostic `separation_margin`: Distance gap ($d_2 - d_1$) between best and runner-up
   zone matches. It is NEVER named or treated as a "confidence" score.
4. `calibration_version`: Records which calibration profile generated this classification.
5. Scoped Exclusively to Spatial State: `system_health` is explicitly excluded (handled by Section 11).
6. Strict Motion Gating: When `is_motion` is False, `zone_id` and `separation_margin` are strictly `None`.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any, Optional


@dataclass(frozen=True, slots=True)
class StateMessage:
    """Versioned wire contract representing published spatial state.

    Attributes:
        schema_version: Monotonically increasing wire protocol schema version (starts at 1).
        timestamp: UTC Unix epoch seconds (time.time()) corresponding to decision publication.
        zone_id: 1-indexed room zone identifier (1 <= zone_id <= zone_count), or None if unknown.
        is_motion: Instantaneous motion detection flag with hysteresis smoothing.
        presence: Motion-derived presence flag sustained by configurable timeout.
        separation_margin: Distance gap between winning zone and runner-up ($d_2 - d_1$).
            None if is_motion=False or single-zone environment.
        calibration_version: Integer version of the active CalibrationProfile, or None if uncalibrated.
    """

    schema_version: int
    timestamp: float
    zone_id: Optional[int]
    is_motion: bool
    presence: bool
    separation_margin: Optional[float]
    calibration_version: Optional[int] = None

    def __post_init__(self) -> None:
        """Validate invariant constraints on StateMessage."""
        if self.schema_version < 1:
            raise ValueError(
                f"schema_version must be >= 1, got {self.schema_version}"
            )
        if self.timestamp <= 0.0:
            raise ValueError(f"timestamp must be positive, got {self.timestamp}")
        if not isinstance(self.is_motion, bool):
            raise ValueError(
                f"is_motion must be a bool, got {type(self.is_motion).__name__}"
            )
        if not isinstance(self.presence, bool):
            raise ValueError(
                f"presence must be a bool, got {type(self.presence).__name__}"
            )

        # Motion gating invariant: no zone or margin without active motion
        if not self.is_motion:
            if self.zone_id is not None:
                raise ValueError(
                    f"zone_id must be None when is_motion is False, got {self.zone_id}"
                )
            if self.separation_margin is not None:
                raise ValueError(
                    f"separation_margin must be None when is_motion is False, got {self.separation_margin}"
                )

        if self.zone_id is not None:
            if not isinstance(self.zone_id, int):
                raise TypeError(
                    f"zone_id must be an int or None, got {type(self.zone_id).__name__}"
                )
            if self.zone_id < 1:
                raise ValueError(f"zone_id must be >= 1, got {self.zone_id}")

        if self.separation_margin is not None:
            if not math.isfinite(self.separation_margin) or self.separation_margin < 0.0:
                raise ValueError(
                    f"separation_margin must be a finite non-negative number, got {self.separation_margin}"
                )

        if self.calibration_version is not None:
            if self.calibration_version < 1:
                raise ValueError(
                    f"calibration_version must be >= 1, got {self.calibration_version}"
                )

    def to_dict(self) -> dict[str, Any]:
        """Convert state message to a JSON-serializable dictionary."""
        return {
            "schema_version": self.schema_version,
            "timestamp": self.timestamp,
            "zone_id": self.zone_id,
            "is_motion": self.is_motion,
            "presence": self.presence,
            "separation_margin": (
                round(self.separation_margin, 6)
                if self.separation_margin is not None
                else None
            ),
            "calibration_version": self.calibration_version,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> StateMessage:
        """Construct StateMessage from a dictionary."""
        zone_id = int(data["zone_id"]) if data.get("zone_id") is not None else None
        sep_margin = (
            float(data["separation_margin"])
            if data.get("separation_margin") is not None
            else None
        )
        cal_ver = (
            int(data["calibration_version"])
            if data.get("calibration_version") is not None
            else None
        )
        return cls(
            schema_version=int(data.get("schema_version", 1)),
            timestamp=float(data["timestamp"]),
            zone_id=zone_id,
            is_motion=bool(data["is_motion"]),
            presence=bool(data["presence"]),
            separation_margin=sep_margin,
            calibration_version=cal_ver,
        )

    def to_json(self) -> str:
        """Serialize state message to a UTF-8 JSON string."""
        return json.dumps(self.to_dict())

    @classmethod
    def from_json(cls, json_str: str) -> StateMessage:
        """Deserialize state message from a JSON string."""
        data = json.loads(json_str)
        return cls.from_dict(data)
