"""Unit and scenario tests for pairwise zone separability analysis."""

import math
from pathlib import Path

import pytest

from wisense_backend.calibration.separability import (
    PairwiseSeparability,
    SeparabilityReport,
    compute_cosine_distance,
    compute_normalized_euclidean_distance,
    compute_separability,
)
from wisense_backend.contracts.calibration import CalibrationProfile, ZoneSignature


class TestSeparabilityMetrics:
    """Validate distance calculation functions and edge conditions."""

    def test_cosine_distance_identical_vectors(self) -> None:
        """Identical vectors must have cosine distance 0.0."""
        vec = [0.1, 0.5, 0.8, 0.2]
        dist = compute_cosine_distance(vec, vec)
        assert dist == pytest.approx(0.0, abs=1e-9)

        # Proportional/scaled vectors have identical shape -> distance 0.0
        vec_scaled = [v * 3.5 for v in vec]
        dist_scaled = compute_cosine_distance(vec, vec_scaled)
        assert dist_scaled == pytest.approx(0.0, abs=1e-9)

    def test_cosine_distance_orthogonal_vectors(self) -> None:
        """Mutually orthogonal vectors must have cosine distance 1.0."""
        vec_a = [1.0, 0.0, 0.0]
        vec_b = [0.0, 1.0, 0.0]
        dist = compute_cosine_distance(vec_a, vec_b)
        assert dist == pytest.approx(1.0, abs=1e-9)

    def test_cosine_distance_zero_vector_edge_cases(self) -> None:
        """Test zero-magnitude vector handling."""
        zeros = [0.0, 0.0, 0.0]
        non_zeros = [1.0, 2.0, 3.0]

        # Both flat zero -> identical shape (0.0)
        assert compute_cosine_distance(zeros, zeros) == pytest.approx(0.0)

        # One flat zero, one non-zero -> maximally distinct (1.0)
        assert compute_cosine_distance(zeros, non_zeros) == pytest.approx(1.0)
        assert compute_cosine_distance(non_zeros, zeros) == pytest.approx(1.0)

    def test_cosine_distance_length_mismatch(self) -> None:
        """Vectors of different dimensions must raise ValueError."""
        with pytest.raises(ValueError, match="Vector length mismatch"):
            compute_cosine_distance([1.0, 2.0], [1.0, 2.0, 3.0])

    def test_normalized_euclidean_distance(self) -> None:
        """Verify normalized Euclidean distance relationship to cosine distance."""
        vec_a = [0.2, 0.8]
        vec_b = [0.8, 0.2]
        cos_dist = compute_cosine_distance(vec_a, vec_b)
        euc_dist = compute_normalized_euclidean_distance(vec_a, vec_b)
        assert euc_dist == pytest.approx(math.sqrt(cos_dist))


class TestPairwiseSeparabilityContract:
    """Validate PairwiseSeparability dataclass validation."""

    def test_canonical_ordering_required(self) -> None:
        """zone_a must be strictly less than zone_b."""
        with pytest.raises(ValueError, match="zone_a < zone_b"):
            PairwiseSeparability(
                zone_a=2,
                zone_b=1,
                metric_name="cosine_distance",
                score=0.5,
                threshold=0.2,
                is_separated=True,
            )
        with pytest.raises(ValueError, match="zone_a < zone_b"):
            PairwiseSeparability(
                zone_a=2,
                zone_b=2,
                metric_name="cosine_distance",
                score=0.5,
                threshold=0.2,
                is_separated=True,
            )

    def test_score_validation(self) -> None:
        """score must be non-negative and finite."""
        with pytest.raises(ValueError, match="score must be a finite non-negative number"):
            PairwiseSeparability(
                zone_a=1,
                zone_b=2,
                metric_name="cosine_distance",
                score=-0.1,
                threshold=0.2,
                is_separated=False,
            )


class TestSeparabilityAnalysis:
    """Validate pairwise separability reporting on well-separated and poorly-separated profiles."""

    def test_well_separated_default_profile(self) -> None:
        """Verify canonical 4-zone profile is detected as fully separable."""
        profile_path = (
            Path(__file__).resolve().parent.parent
            / "config"
            / "calibration_profiles"
            / "default_calibration.json"
        )
        assert profile_path.is_file()
        profile = CalibrationProfile.from_json_file(profile_path)

        report = compute_separability(profile, threshold=0.20, metric="cosine_distance")
        assert report.zone_count == 4
        assert report.threshold == 0.20
        assert report.metric_name == "cosine_distance"
        assert report.is_fully_separable is True
        assert len(report.poorly_separated_pairs) == 0
        assert len(report.well_separated_pairs) == 6  # 4 choose 2 = 6

        # Check summary formatting
        summary = report.summary()
        assert "ZONE SEPARABILITY REPORT" in summary
        assert "OVERALL VERDICT: PASS" in summary
        assert "WELL-SEPARATED" in summary

        # Check dict conversion
        d = report.to_dict()
        assert d["is_fully_separable"] is True
        assert len(d["pairwise_metrics"]) == 6

    def test_deliberately_poorly_separated_pair_is_flagged(self) -> None:
        """Audit item #10 operationalization: Deliberately construct two zones with

        heavily overlapping subcarrier distributions and verify separability analysis
        flags the pair as POORLY-SEPARATED.
        """
        subcarrier_count = 64
        # Zone 1 and Zone 2 have almost identical centers (12.0 vs 12.5) with broad spread (10.0)
        sig1_variances = tuple(
            0.05 + 0.45 * math.exp(-0.5 * ((k - 12.0) / 10.0) ** 2)
            for k in range(subcarrier_count)
        )
        sig2_variances = tuple(
            0.05 + 0.45 * math.exp(-0.5 * ((k - 12.5) / 10.0) ** 2)
            for k in range(subcarrier_count)
        )
        # Zone 3 is far away (center 52.0)
        sig3_variances = tuple(
            0.05 + 0.45 * math.exp(-0.5 * ((k - 52.0) / 10.0) ** 2)
            for k in range(subcarrier_count)
        )

        sig1 = ZoneSignature(1, subcarrier_count, sig1_variances, sum(sig1_variances), 30)
        sig2 = ZoneSignature(2, subcarrier_count, sig2_variances, sum(sig2_variances), 30)
        sig3 = ZoneSignature(3, subcarrier_count, sig3_variances, sum(sig3_variances), 30)

        profile = CalibrationProfile(
            zone_count=3,
            subcarrier_count=subcarrier_count,
            calibration_version=1,
            created_at="2026-09-15T00:00:00Z",
            source="synthetic",
            zone_signatures={1: sig1, 2: sig2, 3: sig3},
        )

        # Evaluate with standard threshold of 0.20
        report = compute_separability(profile, threshold=0.20)

        assert report.is_fully_separable is False
        assert (1, 2) in report.poorly_separated_pairs
        assert (1, 3) in report.well_separated_pairs
        assert (2, 3) in report.well_separated_pairs

        # Verify pair (1, 2) score is very small (< 0.05)
        pair_1_2 = next(m for m in report.pairwise_metrics if m.zone_a == 1 and m.zone_b == 2)
        assert pair_1_2.score < 0.05
        assert pair_1_2.is_separated is False

        # Verify diagnostic summary output
        summary = report.summary()
        assert "OVERALL VERDICT: WARNING" in summary
        assert "Conflict between Zone 1 and Zone 2" in summary
        assert "POORLY-SEPARATED" in summary

    def test_single_zone_profile(self) -> None:
        """A profile with zone_count=1 has zero pairwise combinations and is vacuously separable."""
        sig1 = ZoneSignature(1, 4, (0.1, 0.2, 0.3, 0.4), 1.0, 10)
        profile = CalibrationProfile(
            zone_count=1,
            subcarrier_count=4,
            calibration_version=1,
            created_at="2026-09-15T00:00:00Z",
            source="synthetic",
            zone_signatures={1: sig1},
        )
        report = compute_separability(profile)
        assert report.zone_count == 1
        assert len(report.pairwise_metrics) == 0
        assert report.is_fully_separable is True
        assert "No pairs to evaluate" in report.summary()

    def test_invalid_parameters_raise(self) -> None:
        """Verify parameter bounds enforcement on compute_separability."""
        profile_path = (
            Path(__file__).resolve().parent.parent
            / "config"
            / "calibration_profiles"
            / "default_calibration.json"
        )
        profile = CalibrationProfile.from_json_file(profile_path)

        with pytest.raises(ValueError, match="threshold must be in"):
            compute_separability(profile, threshold=-0.1)
        with pytest.raises(ValueError, match="threshold must be in"):
            compute_separability(profile, threshold=1.5)
        with pytest.raises(ValueError, match="Unknown metric"):
            compute_separability(profile, metric="unsupported_metric")
