"""ZoneCalibrator implementation for WiSense-Twin.

ZoneCalibrator ingests a batch of `LabeledFeatureSample` instances, groups them by
zone_id, computes the empirical reference RF signature (mean per-subcarrier variance)
for each monitored zone, and produces a versioned `CalibrationProfile`.

SCOPE & ARCHITECTURE DISCIPLINE:
--------------------------------
- One-Shot Batch Calibration: This is an offline, batch calibration engine.
  Adaptive/online recalibration is strictly out of scope for this section.
- Generic N-Zone: Accommodates any zone count (N >= 1) and any subcarrier count (K >= 1).
- Strict Validation: Confirms that every configured zone has observed training samples.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Iterable, Sequence

from wisense_backend.calibration.labeled_sample import LabeledFeatureSample
from wisense_backend.contracts.calibration import CalibrationProfile, ZoneSignature


class ZoneCalibrator:
    """Offline batch engine for deriving a CalibrationProfile from labeled feature samples.

    Attributes:
        zone_count: Expected number of monitored physical zones (1 .. zone_count).
        subcarrier_count: Expected number of OFDM subcarriers per feature vector.
        calibration_version: Version counter to tag the produced profile.
        source: Provenance tag ("synthetic" or "hardware").
    """

    def __init__(
        self,
        zone_count: int,
        subcarrier_count: int,
        calibration_version: int = 1,
        source: str = "synthetic",
    ) -> None:
        if zone_count < 1:
            raise ValueError(f"zone_count must be >= 1, got {zone_count}")
        if subcarrier_count < 1:
            raise ValueError(f"subcarrier_count must be >= 1, got {subcarrier_count}")
        if calibration_version < 1:
            raise ValueError(
                f"calibration_version must be >= 1, got {calibration_version}"
            )
        valid_sources = {"synthetic", "hardware"}
        if source not in valid_sources:
            raise ValueError(f"source must be one of {valid_sources}, got '{source}'")

        self._zone_count = zone_count
        self._subcarrier_count = subcarrier_count
        self._calibration_version = calibration_version
        self._source = source

    @property
    def zone_count(self) -> int:
        return self._zone_count

    @property
    def subcarrier_count(self) -> int:
        return self._subcarrier_count

    @property
    def calibration_version(self) -> int:
        return self._calibration_version

    @property
    def source(self) -> str:
        return self._source

    def calibrate(
        self,
        samples: Iterable[LabeledFeatureSample],
        created_at: str | None = None,
    ) -> CalibrationProfile:
        """Process labeled feature samples and build a CalibrationProfile.

        Args:
            samples: Stream or sequence of `LabeledFeatureSample` instances.
            created_at: Optional UTC ISO 8601 timestamp string. If omitted, current UTC time is used.

        Returns:
            A validated, immutable `CalibrationProfile`.

        Raises:
            ValueError: If any configured zone has no samples, if a sample exceeds zone_count,
                or if subcarrier counts mismatch.
        """
        zone_samples: dict[int, list[LabeledFeatureSample]] = defaultdict(list)

        sample_counter = 0
        for sample in samples:
            sample_counter += 1
            fv = sample.feature_vector

            if fv.subcarrier_count != self._subcarrier_count:
                raise ValueError(
                    f"Sample {sample_counter} has subcarrier_count {fv.subcarrier_count}, "
                    f"expected {self._subcarrier_count}"
                )

            # Skip unassigned / quiet baseline samples; ZoneCalibrator derives reference signatures
            # exclusively from confirmed motion intervals in monitored zones.
            if sample.zone_id is None:
                continue

            if sample.zone_id > self._zone_count:
                raise ValueError(
                    f"Sample {sample_counter} has zone_id {sample.zone_id}, "
                    f"which exceeds configured zone_count {self._zone_count}"
                )
            zone_samples[sample.zone_id].append(sample)

        # Ensure all configured zones (1 .. zone_count) have labeled samples
        missing_zones = [z for z in range(1, self._zone_count + 1) if z not in zone_samples or len(zone_samples[z]) == 0]
        if missing_zones:
            raise ValueError(
                f"Cannot calibrate profile: missing labeled samples for zone(s): {missing_zones}. "
                f"All {self._zone_count} configured zones must have training samples."
            )

        # Compute ZoneSignature for each zone
        signatures: dict[int, ZoneSignature] = {}
        for z in range(1, self._zone_count + 1):
            samples_for_z = zone_samples[z]
            n_samples = len(samples_for_z)

            # Element-wise mean of per-subcarrier variance
            accum_variance = [0.0] * self._subcarrier_count
            accum_aggregate = 0.0

            for s in samples_for_z:
                for sc_idx, var_val in enumerate(s.feature_vector.variance_per_subcarrier):
                    accum_variance[sc_idx] += var_val
                accum_aggregate += s.feature_vector.aggregate_variance

            mean_variance = tuple(val / n_samples for val in accum_variance)
            mean_aggregate = accum_aggregate / n_samples

            signatures[z] = ZoneSignature(
                zone_id=z,
                subcarrier_count=self._subcarrier_count,
                mean_variance_per_subcarrier=mean_variance,
                mean_aggregate_variance=mean_aggregate,
                sample_count=n_samples,
            )

        timestamp = created_at or datetime.now(timezone.utc).isoformat()

        return CalibrationProfile(
            zone_count=self._zone_count,
            subcarrier_count=self._subcarrier_count,
            calibration_version=self._calibration_version,
            created_at=timestamp,
            source=self._source,
            zone_signatures=signatures,
        )
