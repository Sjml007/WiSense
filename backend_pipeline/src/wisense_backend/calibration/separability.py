"""Pairwise zone separability analysis for WiSense-Twin.

This module computes pairwise RF separability metrics between zone signatures in a
`CalibrationProfile` to evaluate whether room zones have distinct spatial multipath
characteristics or suffer from high mutual interference / spectral overlap.

AUDIT ITEM #10 — OPERATIONALIZATION:
-------------------------------------
Per the architecture audit, zone separability cannot be assumed — it must be measured.
If two adjacent zones activate similar subcarrier patterns, spatial ambiguity will cause
the runtime zone classifier (Section 8) to confuse them.
This module turns that risk into a quantifiable diagnostic report:
- Zone pairs with separability >= threshold are marked WELL-SEPARATED.
- Zone pairs with separability < threshold are explicitly flagged as POORLY-SEPARATED.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Callable, Sequence

from wisense_backend.contracts.calibration import CalibrationProfile, ZoneSignature


def compute_cosine_distance(vec_a: Sequence[float], vec_b: Sequence[float]) -> float:
    """Compute cosine distance (1.0 - cosine_similarity) between two non-negative vectors.

    For RF subcarrier variance vectors, cosine distance measures the dissimilarity of
    spectral shape across subcarriers, invariant to overall motion amplitude/intensity.

    Returns:
        A float in [0.0, 1.0], where:
        - 0.0 means identical relative spectral shape (completely indistinguishable).
        - 1.0 means perfectly orthogonal subcarrier activation (maximally separable).
    """
    if len(vec_a) != len(vec_b):
        raise ValueError(
            f"Vector length mismatch: len(vec_a)={len(vec_a)} vs len(vec_b)={len(vec_b)}"
        )

    dot_product = 0.0
    norm_a_sq = 0.0
    norm_b_sq = 0.0

    for a, b in zip(vec_a, vec_b):
        dot_product += a * b
        norm_a_sq += a * a
        norm_b_sq += b * b

    norm_a = math.sqrt(norm_a_sq)
    norm_b = math.sqrt(norm_b_sq)

    # Edge cases: zero vectors
    if norm_a == 0.0 and norm_b == 0.0:
        return 0.0  # Identical flat zeros
    if norm_a == 0.0 or norm_b == 0.0:
        return 1.0  # One is flat zero, other has energy -> maximally distinct

    similarity = dot_product / (norm_a * norm_b)
    # Clamp to [0.0, 1.0] for numerical safety with non-negative variances
    similarity = max(0.0, min(1.0, similarity))
    return 1.0 - similarity


def compute_normalized_euclidean_distance(
    vec_a: Sequence[float], vec_b: Sequence[float]
) -> float:
    """Compute normalized Euclidean distance between L2-normalized vectors.

    Returns:
        A float in [0.0, 1.0].
    """
    cosine_dist = compute_cosine_distance(vec_a, vec_b)
    # ||u_hat - v_hat|| / sqrt(2) = sqrt(1 - cos_sim) = sqrt(cosine_dist)
    return math.sqrt(cosine_dist)


METRIC_FUNCTIONS: dict[str, Callable[[Sequence[float], Sequence[float]], float]] = {
    "cosine_distance": compute_cosine_distance,
    "normalized_euclidean": compute_normalized_euclidean_distance,
}


@dataclass(frozen=True, slots=True)
class PairwiseSeparability:
    """Separability evaluation between a pair of zones.

    Attributes:
        zone_a: First zone ID (1-indexed, a < b).
        zone_b: Second zone ID (1-indexed, a < b).
        metric_name: Name of the distance metric used.
        score: Computed dissimilarity score in [0.0, 1.0]. Higher = better separated.
        threshold: Threshold applied to determine acceptability.
        is_separated: True if score >= threshold, False if poorly separated.
    """

    zone_a: int
    zone_b: int
    metric_name: str
    score: float
    threshold: float
    is_separated: bool

    def __post_init__(self) -> None:
        if self.zone_a >= self.zone_b:
            raise ValueError(
                f"Canonical pair order requires zone_a < zone_b, got {self.zone_a} >= {self.zone_b}"
            )
        if not math.isfinite(self.score) or self.score < 0.0:
            raise ValueError(
                f"score must be a finite non-negative number, got {self.score}"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "zone_a": self.zone_a,
            "zone_b": self.zone_b,
            "metric_name": self.metric_name,
            "score": round(self.score, 4),
            "threshold": self.threshold,
            "is_separated": self.is_separated,
        }


@dataclass(frozen=True, slots=True)
class SeparabilityReport:
    """Comprehensive diagnostic report summarizing pairwise zone separability.

    Attributes:
        calibration_version: Calibration version number evaluated.
        zone_count: Number of zones analyzed.
        threshold: Minimum acceptable distance score for a pair to be deemed separated.
        metric_name: Name of the distance metric utilized.
        pairwise_metrics: Tuple of all evaluated pairwise comparisons.
        poorly_separated_pairs: Tuple of (zone_a, zone_b) tuples falling below threshold.
        well_separated_pairs: Tuple of (zone_a, zone_b) tuples meeting or exceeding threshold.
        is_fully_separable: True if ALL zone pairs are well-separated; False if any fail.
    """

    calibration_version: int
    zone_count: int
    threshold: float
    metric_name: str
    pairwise_metrics: tuple[PairwiseSeparability, ...]
    poorly_separated_pairs: tuple[tuple[int, int], ...]
    well_separated_pairs: tuple[tuple[int, int], ...]
    is_fully_separable: bool

    def summary(self) -> str:
        """Generate a human-readable diagnostic text summary of the separability report."""
        lines = [
            "=" * 68,
            f"ZONE SEPARABILITY REPORT (Profile Version {self.calibration_version})",
            f"Zones: {self.zone_count} | Metric: {self.metric_name} | Threshold: {self.threshold:.2f}",
            "-" * 68,
            f"{'Pair':<12} | {'Score':<10} | {'Status':<16} | {'Separable?':<10}",
            "-" * 68,
        ]

        if not self.pairwise_metrics:
            lines.append("No pairs to evaluate (zone_count < 2).")
        else:
            for m in self.pairwise_metrics:
                status_str = "WELL-SEPARATED" if m.is_separated else "POORLY-SEPARATED"
                lines.append(
                    f"Zone {m.zone_a} - {m.zone_b:<4} | {m.score:<10.4f} | {status_str:<16} | {'YES' if m.is_separated else 'NO':<10}"
                )

        lines.append("-" * 68)
        if self.is_fully_separable:
            lines.append("OVERALL VERDICT: PASS -- All zone pairs meet the separability threshold.")
        else:
            lines.append(
                f"OVERALL VERDICT: WARNING -- {len(self.poorly_separated_pairs)} zone pair(s) "
                f"suffer from severe spectral overlap and cannot be reliably resolved."
            )
            for za, zb in self.poorly_separated_pairs:
                lines.append(f"  * Conflict between Zone {za} and Zone {zb}")
        lines.append("=" * 68)
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        """Serialize report to a dictionary."""
        return {
            "calibration_version": self.calibration_version,
            "zone_count": self.zone_count,
            "threshold": self.threshold,
            "metric_name": self.metric_name,
            "is_fully_separable": self.is_fully_separable,
            "pairwise_metrics": [m.to_dict() for m in self.pairwise_metrics],
            "poorly_separated_pairs": [list(p) for p in self.poorly_separated_pairs],
            "well_separated_pairs": [list(p) for p in self.well_separated_pairs],
        }


def compute_separability(
    profile: CalibrationProfile,
    threshold: float = 0.20,
    metric: str = "cosine_distance",
) -> SeparabilityReport:
    """Evaluate pairwise separability between all zones in a CalibrationProfile.

    Args:
        profile: The `CalibrationProfile` to evaluate.
        threshold: Minimum dissimilarity score (in [0.0, 1.0]) required to classify
            a zone pair as adequately separated. Defaults to 0.20.
        metric: Metric key, either "cosine_distance" or "normalized_euclidean".

    Returns:
        A `SeparabilityReport` detailing individual pair scores and overall verdict.

    Raises:
        ValueError: If threshold is negative or metric is unrecognized.
    """
    if threshold < 0.0 or threshold > 1.0:
        raise ValueError(f"threshold must be in [0.0, 1.0], got {threshold}")
    if metric not in METRIC_FUNCTIONS:
        raise ValueError(
            f"Unknown metric '{metric}'. Supported metrics: {list(METRIC_FUNCTIONS.keys())}"
        )

    metric_fn = METRIC_FUNCTIONS[metric]
    pairwise_list: list[PairwiseSeparability] = []
    poor_list: list[tuple[int, int]] = []
    well_list: list[tuple[int, int]] = []

    zones = sorted(profile.zone_signatures.keys())
    for i in range(len(zones)):
        for j in range(i + 1, len(zones)):
            za = zones[i]
            zb = zones[j]
            sig_a = profile.get_signature(za)
            sig_b = profile.get_signature(zb)

            score = metric_fn(
                sig_a.mean_variance_per_subcarrier,
                sig_b.mean_variance_per_subcarrier,
            )
            is_sep = score >= threshold

            pair_eval = PairwiseSeparability(
                zone_a=za,
                zone_b=zb,
                metric_name=metric,
                score=score,
                threshold=threshold,
                is_separated=is_sep,
            )
            pairwise_list.append(pair_eval)

            if is_sep:
                well_list.append((za, zb))
            else:
                poor_list.append((za, zb))

    is_fully = len(poor_list) == 0

    return SeparabilityReport(
        calibration_version=profile.calibration_version,
        zone_count=profile.zone_count,
        threshold=threshold,
        metric_name=metric,
        pairwise_metrics=tuple(pairwise_list),
        poorly_separated_pairs=tuple(poor_list),
        well_separated_pairs=tuple(well_list),
        is_fully_separable=is_fully,
    )
