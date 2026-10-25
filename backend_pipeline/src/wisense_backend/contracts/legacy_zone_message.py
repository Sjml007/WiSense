"""Legacy Unreal zone-protocol message contract for WiSense-Twin.

Defines the frozen `LegacyZoneMessage` dataclass representing an incremental zone
update using the compact 2-field JSON wire format:
    {"zone": <int>, "occupied": <bool>}

SEMANTIC NOTICE:
The field name `occupied` is a legacy wire name derived from the historical Unreal prototype.
It denotes "motion-derived presence held in this zone", NOT confirmed human occupancy detection.
Stationary individuals without active motion will decay to occupied=false upon timeout.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class LegacyZoneMessage:
    """Incremental zone update message for the legacy Unreal prototype protocol.

    Wire format:
        {"zone": <int>, "occupied": <bool>}

    Attributes:
        zone: 1-indexed spatial zone identifier (must be an integer >= 1).
        occupied: Motion-derived presence indicator for the zone (must be a bool).
    """

    zone: int
    occupied: bool

    def __post_init__(self) -> None:
        """Validate legacy zone message fields and types."""
        if isinstance(self.zone, bool) or not isinstance(self.zone, int):
            raise TypeError(f"zone must be an integer, got {type(self.zone).__name__}")
        if self.zone < 1:
            raise ValueError(f"zone must be >= 1, got {self.zone}")
        if not isinstance(self.occupied, bool):
            raise TypeError(f"occupied must be a boolean, got {type(self.occupied).__name__}")

    def to_json(self) -> str:
        """Serialize message to exact compact JSON with no whitespace.

        Returns:
            Compact JSON string with exactly two keys: {"zone":<int>,"occupied":<bool>}.
        """
        return json.dumps({"zone": self.zone, "occupied": self.occupied}, separators=(",", ":"))

    def to_dict(self) -> dict[str, Any]:
        """Convert message to a dictionary representation."""
        return {"zone": self.zone, "occupied": self.occupied}

    @classmethod
    def from_json(cls, raw: str) -> LegacyZoneMessage:
        """Parse LegacyZoneMessage from JSON wire string.

        Args:
            raw: JSON string payload.

        Returns:
            Parsed LegacyZoneMessage instance.

        Raises:
            ValueError: If payload is not an object or does not have exactly the two required keys.
            TypeError: If field types are invalid.
        """
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError(f"Legacy zone payload must be a JSON object, got {type(data).__name__}")
        if set(data.keys()) != {"zone", "occupied"}:
            raise ValueError(
                f"Legacy zone payload must contain exactly keys 'zone' and 'occupied', got {set(data.keys())}"
            )
        return cls(zone=data["zone"], occupied=data["occupied"])
