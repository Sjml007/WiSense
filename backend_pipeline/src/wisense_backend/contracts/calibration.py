"""Calibration contracts and data structures for WiSense-Twin.

This module defines:
- `ZoneSignature`: The immutable reference RF signature (mean per-subcarrier variance)
  characterizing RF perturbation in a specific physical room zone during motion.
- `CalibrationProfile`: A versioned, serializable collection of `ZoneSignature` instances
  for all configured zones in a monitored environment.

DESIGN PRINCIPLES:
------------------
1. Ground-Truth Isolation: Calibration contracts are consumption-only representations of
   reference signatures. They do not leak into online pipeline components (ingestion,
   preprocessing, feature extraction, motion detection), which remain strictly ground-truth-blind.
2. Generic N-Zone Support: Profiles work for any zone count (N >= 1) and any subcarrier
   count (K >= 1). No zone count is ever hardcoded.
3. Versioned & Persistent Artifacts: Profiles can be serialized to and loaded from JSON files
   with explicit versioning, timestamping, and source attribution ("synthetic" vs "hardware").
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import math
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping


@dataclass(frozen=True, slots=True)
class ZoneSignature:
    """Immutable reference RF signature for a specific monitored zone.

    Attributes:
        zone_id: 1-indexed room zone identifier (1 <= zone_id <= zone_count).
        subcarrier_count: Total number of OFDM subcarriers in this signature.
        mean_variance_per_subcarrier: Element-wise mean variance across subcarriers
            computed from labeled training samples during motion in this zone.
        mean_aggregate_variance: Mean overall RF energy across all subcarriers during motion.
        sample_count: Number of labeled feature samples that contributed to this signature.
    """

    zone_id: int
    subcarrier_count: int
    mean_variance_per_subcarrier: tuple[float, ...]
    mean_aggregate_variance: float
    sample_count: int

    def __post_init__(self) -> None:
        """Validate invariant constraints on ZoneSignature attributes."""
        if self.zone_id < 1:
            raise ValueError(f"zone_id must be >= 1 (1-indexed), got {self.zone_id}")
        if self.subcarrier_count < 1:
            raise ValueError(f"subcarrier_count must be >= 1, got {self.subcarrier_count}")
        if len(self.mean_variance_per_subcarrier) != self.subcarrier_count:
            raise ValueError(
                f"mean_variance_per_subcarrier length ({len(self.mean_variance_per_subcarrier)}) "
                f"does not match subcarrier_count ({self.subcarrier_count})"
            )
        for idx, val in enumerate(self.mean_variance_per_subcarrier):
            if not math.isfinite(val) or val < 0.0:
                raise ValueError(
                    f"mean_variance_per_subcarrier[{idx}] must be a finite non-negative number, got {val}"
                )
        if not math.isfinite(self.mean_aggregate_variance) or self.mean_aggregate_variance < 0.0:
            raise ValueError(
                f"mean_aggregate_variance must be a finite non-negative number, got {self.mean_aggregate_variance}"
            )
        if self.sample_count < 1:
            raise ValueError(f"sample_count must be >= 1, got {self.sample_count}")

    def to_dict(self) -> dict[str, Any]:
        """Convert signature to a JSON-serializable dictionary."""
        return {
            "zone_id": self.zone_id,
            "subcarrier_count": self.subcarrier_count,
            "mean_variance_per_subcarrier": list(self.mean_variance_per_subcarrier),
            "mean_aggregate_variance": self.mean_aggregate_variance,
            "sample_count": self.sample_count,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ZoneSignature:
        """Construct a ZoneSignature from a dictionary."""
        return cls(
            zone_id=int(data["zone_id"]),
            subcarrier_count=int(data["subcarrier_count"]),
            mean_variance_per_subcarrier=tuple(
                float(v) for v in data["mean_variance_per_subcarrier"]
            ),
            mean_aggregate_variance=float(data["mean_aggregate_variance"]),
            sample_count=int(data["sample_count"]),
        )


@dataclass(frozen=True, slots=True)
class CalibrationProfile:
    """Versioned calibration configuration containing reference signatures for all zones.

    Attributes:
        zone_count: Number of monitored physical zones in the environment.
        subcarrier_count: Number of OFDM subcarriers analyzed.
        calibration_version: Integer version counter, incremented on each recalibration.
        created_at: UTC ISO 8601 timestamp string of when the calibration was conducted.
        source: Provenance of the calibration data ("synthetic" or "hardware").
        zone_signatures: Mapping from zone_id (int) to the corresponding ZoneSignature.
    """

    zone_count: int
    subcarrier_count: int
    calibration_version: int
    created_at: str
    source: str
    zone_signatures: Mapping[int, ZoneSignature]

    def __post_init__(self) -> None:
        """Validate invariant constraints on CalibrationProfile attributes."""
        if self.zone_count < 1:
            raise ValueError(f"zone_count must be >= 1, got {self.zone_count}")
        if self.subcarrier_count < 1:
            raise ValueError(f"subcarrier_count must be >= 1, got {self.subcarrier_count}")
        if self.calibration_version < 1:
            raise ValueError(
                f"calibration_version must be >= 1, got {self.calibration_version}"
            )
        valid_sources = {"synthetic", "hardware"}
        if self.source not in valid_sources:
            raise ValueError(
                f"source must be one of {valid_sources}, got '{self.source}'"
            )
        if len(self.zone_signatures) != self.zone_count:
            raise ValueError(
                f"zone_signatures count ({len(self.zone_signatures)}) does not match "
                f"zone_count ({self.zone_count})"
            )
        for z in range(1, self.zone_count + 1):
            if z not in self.zone_signatures:
                raise ValueError(f"Missing ZoneSignature for zone_id {z}")
            sig = self.zone_signatures[z]
            if sig.zone_id != z:
                raise ValueError(
                    f"Signature keyed under zone {z} has mismatched zone_id {sig.zone_id}"
                )
            if sig.subcarrier_count != self.subcarrier_count:
                raise ValueError(
                    f"Signature for zone {z} has subcarrier_count {sig.subcarrier_count} "
                    f"which does not match profile subcarrier_count {self.subcarrier_count}"
                )

        # Wrap in MappingProxyType to guarantee genuine post-construction immutability
        object.__setattr__(
            self,
            "zone_signatures",
            MappingProxyType(dict(self.zone_signatures)),
        )

    def get_signature(self, zone_id: int) -> ZoneSignature:
        """Retrieve reference signature for a given 1-indexed zone."""
        if zone_id not in self.zone_signatures:
            raise KeyError(
                f"Zone {zone_id} not found in calibration profile (available: {list(self.zone_signatures.keys())})"
            )
        return self.zone_signatures[zone_id]

    def to_dict(self) -> dict[str, Any]:
        """Convert profile to a JSON-serializable dictionary."""
        return {
            "zone_count": self.zone_count,
            "subcarrier_count": self.subcarrier_count,
            "calibration_version": self.calibration_version,
            "created_at": self.created_at,
            "source": self.source,
            "zone_signatures": {
                str(z): sig.to_dict() for z, sig in self.zone_signatures.items()
            },
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CalibrationProfile:
        """Construct a CalibrationProfile from a dictionary."""
        signatures_raw: Mapping[str, Any] = data.get("zone_signatures", {})
        signatures = {
            int(k): ZoneSignature.from_dict(v) for k, v in signatures_raw.items()
        }
        return cls(
            zone_count=int(data["zone_count"]),
            subcarrier_count=int(data["subcarrier_count"]),
            calibration_version=int(data.get("calibration_version", 1)),
            created_at=str(data.get("created_at", datetime.now(timezone.utc).isoformat())),
            source=str(data.get("source", "synthetic")),
            zone_signatures=signatures,
        )

    def to_json_file(self, file_path: str | Path) -> None:
        """Save calibration profile to a JSON file."""
        path = Path(file_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, indent=2)

    @classmethod
    def from_json_file(cls, file_path: str | Path) -> CalibrationProfile:
        """Load calibration profile from a JSON file."""
        path = Path(file_path)
        if not path.is_file():
            raise FileNotFoundError(f"Calibration profile not found: {path}")
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return cls.from_dict(data)
