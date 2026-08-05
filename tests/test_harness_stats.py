"""Pure-Python statistics used by the parity harness (tests/helpers/stats.py)
— verified against known-answer cases before anything in the parity
harness is allowed to trust them.
"""

from __future__ import annotations

import pytest

from tests.helpers.stats import (
    band_agreement,
    cluster_count,
    compute_tolerance_from_noise_floor,
    pairwise_coclustering_agreement,
    spearman_rank_correlation,
)


def test_spearman_perfect_agreement_is_one():
    assert spearman_rank_correlation([1, 2, 3, 4, 5], [10, 20, 30, 40, 50]) == pytest.approx(1.0)


def test_spearman_perfect_inversion_is_negative_one():
    assert spearman_rank_correlation([1, 2, 3, 4, 5], [5, 4, 3, 2, 1]) == pytest.approx(-1.0)


def test_spearman_handles_ties_via_average_rank():
    # llm_score is an integer 1-10 in practice — ties are the norm, not
    # an edge case (p3-contract.md §9.2 band comparisons rely on this).
    a = [5, 5, 5, 8, 8]
    b = [1, 2, 3, 4, 5]
    rho = spearman_rank_correlation(a, b)
    assert -1.0 <= rho <= 1.0  # doesn't raise, produces a sane value despite heavy ties


def test_spearman_constant_series_returns_zero_not_nan():
    assert spearman_rank_correlation([5, 5, 5], [1, 2, 3]) == 0.0


def test_spearman_length_mismatch_raises():
    with pytest.raises(ValueError):
        spearman_rank_correlation([1, 2], [1, 2, 3])


def test_spearman_degenerate_single_point_is_zero():
    assert spearman_rank_correlation([1], [1]) == 0.0


def test_band_agreement_all_match():
    assert band_agreement(["red", "green"], ["red", "green"]) == 1.0


def test_band_agreement_partial():
    assert band_agreement(["red", "green", "magenta"], ["red", "yellow", "magenta"]) == pytest.approx(2 / 3)


def test_band_agreement_empty_is_vacuously_one():
    assert band_agreement([], []) == 1.0


def test_band_agreement_length_mismatch_raises():
    with pytest.raises(ValueError):
        band_agreement(["red"], ["red", "green"])


def test_pairwise_coclustering_agreement_identical_partitions():
    a = {"1": "g1", "2": "g1", "3": "g2"}
    b = {"1": "g1", "2": "g1", "3": "g2"}
    assert pairwise_coclustering_agreement(a, b) == 1.0


def test_pairwise_coclustering_agreement_ignores_label_names():
    # Same PARTITION, different arbitrary cluster ids — p3-contract.md §9.2:
    # "co-located... not necessarily folded identically".
    a = {"1": 100, "2": 100, "3": 200}
    b = {"1": "alpha", "2": "alpha", "3": "beta"}
    assert pairwise_coclustering_agreement(a, b) == 1.0


def test_pairwise_coclustering_agreement_totally_different_partitions():
    a = {"1": "g1", "2": "g1", "3": "g1"}  # all one cluster
    b = {"1": "g1", "2": "g2", "3": "g3"}  # all singletons
    # every pair disagrees: A says same-cluster, B says different-cluster
    assert pairwise_coclustering_agreement(a, b) == 0.0


def test_pairwise_coclustering_none_and_empty_string_are_singletons():
    a = {"1": None, "2": None, "3": "g1"}
    b = {"1": "", "2": "", "3": "g1"}
    # A: 1 and 2 are each their own singleton (None != None-as-cluster);
    # B: same structure with "" instead of None — partitions equivalent.
    assert pairwise_coclustering_agreement(a, b) == 1.0


def test_pairwise_coclustering_mismatched_keys_raises():
    with pytest.raises(ValueError):
        pairwise_coclustering_agreement({"1": "g1"}, {"2": "g1"})


def test_pairwise_coclustering_fewer_than_two_items_is_trivially_one():
    assert pairwise_coclustering_agreement({}, {}) == 1.0
    assert pairwise_coclustering_agreement({"1": "g1"}, {"1": "g2"}) == 1.0


def test_cluster_count_counts_singletons_individually():
    labels = {"1": "g1", "2": "g1", "3": None, "4": ""}
    assert cluster_count(labels) == 3  # g1, and two independent singletons


def test_compute_tolerance_from_noise_floor_subtracts_margin():
    result = compute_tolerance_from_noise_floor(0.9, 0.95, margin=0.05)
    assert result["rank_correlation_threshold"] == pytest.approx(0.85)
    assert result["band_agreement_threshold"] == pytest.approx(0.90)


def test_compute_tolerance_from_noise_floor_never_goes_negative():
    result = compute_tolerance_from_noise_floor(0.02, 0.01, margin=0.05)
    assert result["rank_correlation_threshold"] == 0.0
    assert result["band_agreement_threshold"] == 0.0


def test_compute_tolerance_from_noise_floor_default_margin_is_small():
    result = compute_tolerance_from_noise_floor(1.0, 1.0)
    # Default margin should be a small forgiveness buffer, not zero and
    # not huge — pin it loosely so a future tuning doesn't silently
    # produce something absurd (e.g. margin > 0.2) without this failing.
    assert 0.0 < (1.0 - result["rank_correlation_threshold"]) <= 0.2
