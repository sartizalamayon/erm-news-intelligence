"""ADR §9.1's MODEL-DEPENDENT half of the redefined P3 parity contract:

    "Tolerance-tested: score agreement measured by rank correlation
    against the legacy 70B run on the frozen sample, floor to be set once
    the legacy noise floor is measured (§7.2)."
    "Manually reviewed by the orchestrator: every sample falling outside
    tolerance."

`tests.helpers.parity.check_score_tolerance()` takes the threshold as a
REQUIRED, caller-supplied argument — never a hardcoded constant (mission:
"the correct value is unknown until #4 runs"). These tests exercise it
with EXPLICIT example thresholds so the mechanism is proven correct; none
of them assert what the REAL threshold should be — that number doesn't
exist yet (see docs/test-strategy.md's noise-floor section).

Also exercises the harness against the REAL frozen p3_golden fixture,
using golden-vs-golden (an intentionally trivial 'perfect rebuild') and a
seeded synthetic perturbation of it (a 'rebuild that mostly but not
entirely agrees') so the report format is demonstrated on real sample ids
and real score distributions, not toy data.
"""

from __future__ import annotations

import random

import pytest

from tests.fixtures.loaders import load_p3_golden
from tests.helpers.parity import (
    ToleranceReport,
    check_score_tolerance,
    format_tolerance_report,
    score_band,
)


def _rows(n=3):
    return [{"id": i, "llm_score": s} for i, s in zip(range(n), [8, 3, 6])]


def test_identical_scores_pass_any_sane_threshold():
    golden = _rows()
    report = check_score_tolerance(
        golden, golden, rank_correlation_threshold=0.9, band_agreement_threshold=0.9
    )
    assert report.rank_correlation == pytest.approx(1.0)
    assert report.band_agreement == 1.0
    assert report.passed is True
    assert report.outliers == []


def test_band_crossing_disagreement_is_listed_as_an_outlier():
    golden = [{"id": 1, "llm_score": 9}, {"id": 2, "llm_score": 5}]
    rebuild = [{"id": 1, "llm_score": 9}, {"id": 2, "llm_score": 2}]  # green -> magenta
    report = check_score_tolerance(
        golden, rebuild, rank_correlation_threshold=0.0, band_agreement_threshold=0.0
    )
    assert len(report.outliers) == 1
    outlier = report.outliers[0]
    assert outlier.record_id == 2
    assert outlier.golden_band == "green"
    assert outlier.rebuild_band == "magenta"


def test_report_fails_when_below_threshold_even_with_no_band_crossings():
    """Rank correlation and band agreement are independent checks — a
    report can fail on correlation alone even if every band happens to
    still agree (e.g. systematically compressed scores that never cross a
    band boundary but no longer RANK the same).
    """
    golden = [{"id": i, "llm_score": s} for i, s in enumerate([9, 8, 7, 6])]
    rebuild = [{"id": i, "llm_score": s} for i, s in enumerate([6, 9, 7, 8])]  # reordered, same set
    report = check_score_tolerance(
        golden, rebuild, rank_correlation_threshold=0.95, band_agreement_threshold=0.0
    )
    assert report.rank_correlation < 0.95
    assert report.passed is False


def test_only_overlapping_ids_are_compared():
    golden = [{"id": 1, "llm_score": 8}, {"id": 2, "llm_score": 4}]
    rebuild = [{"id": 1, "llm_score": 8}, {"id": 3, "llm_score": 9}]  # id 3 has no golden counterpart
    report = check_score_tolerance(
        golden, rebuild, rank_correlation_threshold=0.0, band_agreement_threshold=0.0
    )
    assert report.n == 1  # only id=1 overlaps


def test_no_overlap_at_all_raises_rather_than_reporting_a_meaningless_pass():
    golden = [{"id": 1, "llm_score": 8}]
    rebuild = [{"id": 2, "llm_score": 8}]
    with pytest.raises(ValueError):
        check_score_tolerance(golden, rebuild, rank_correlation_threshold=0.0, band_agreement_threshold=0.0)


def test_threshold_is_a_required_argument_not_a_default():
    """Guards the mission's explicit non-negotiable: calling this without
    thresholds must be a TypeError (Python enforces the required keyword-
    only args), not a silent fallback to some baked-in number.
    """
    import inspect

    sig = inspect.signature(check_score_tolerance)
    assert sig.parameters["rank_correlation_threshold"].default is inspect.Parameter.empty
    assert sig.parameters["band_agreement_threshold"].default is inspect.Parameter.empty


def test_format_tolerance_report_shows_pass_fail_per_metric_and_outliers():
    report = ToleranceReport(
        n=2,
        rank_correlation=0.5,
        rank_correlation_threshold=0.8,
        band_agreement=1.0,
        band_agreement_threshold=0.9,
        outliers=[],
    )
    text = format_tolerance_report(report)
    assert "rank correlation" in text
    assert "FAIL" in text  # correlation 0.5 < threshold 0.8
    assert "PASS" in text  # band agreement 1.0 >= threshold 0.9
    assert "overall: FAIL" in text


def test_format_tolerance_report_lists_every_outlier_id_for_manual_review():
    from tests.helpers.parity import ToleranceOutlier

    outlier = ToleranceOutlier(
        record_id=42, golden_score=9, rebuild_score=4, golden_band="red", rebuild_band="magenta"
    )
    report = ToleranceReport(
        n=1,
        rank_correlation=1.0,
        rank_correlation_threshold=0.5,
        band_agreement=0.0,
        band_agreement_threshold=0.9,
        outliers=[outlier],
    )
    text = format_tolerance_report(report)
    assert "id=42" in text
    assert "red" in text and "magenta" in text


# ── Exercised against the real frozen sample ────────────────────────────


def _real_golden_score_rows():
    return [{"id": row["id"], "llm_score": row["llm_score"]} for row in load_p3_golden()["rows"]]


def test_self_comparison_against_real_frozen_sample_is_perfect():
    golden = _real_golden_score_rows()
    report = check_score_tolerance(
        golden, golden, rank_correlation_threshold=0.99, band_agreement_threshold=0.99
    )
    assert report.n == 100
    assert report.rank_correlation == pytest.approx(1.0)
    assert report.band_agreement == 1.0
    assert report.passed is True


def test_seeded_perturbation_of_real_sample_produces_a_readable_report():
    """A deterministic, seeded perturbation stands in for 'a rebuild that
    mostly agrees but sometimes doesn't' — not a claim about what a real
    rebuild will actually score. Demonstrates the report format end to
    end against real sample ids.
    """
    golden = _real_golden_score_rows()
    rng = random.Random(42)
    rebuild = []
    for row in golden:
        score = row["llm_score"]
        if rng.random() < 0.15:  # ~15% of rows get perturbed
            score = max(1.0, min(10.0, score + rng.choice([-3, 3])))
        rebuild.append({"id": row["id"], "llm_score": score})

    report = check_score_tolerance(
        golden, rebuild, rank_correlation_threshold=0.8, band_agreement_threshold=0.8
    )
    assert 0.0 <= report.rank_correlation <= 1.0
    assert 0.0 <= report.band_agreement <= 1.0
    text = format_tolerance_report(report)
    assert f"n={report.n}" in text
    # every outlier reported must correspond to a real frozen-sample id
    golden_ids = {row["id"] for row in golden}
    assert all(o.record_id in golden_ids for o in report.outliers)


def test_score_band_matches_p3_contract_cutoffs_at_every_boundary():
    # p3-contract.md §2.2 / main.py:172-176.
    assert score_band(10) == "red"
    assert score_band(8) == "red"
    assert score_band(7.99) == "yellow"
    assert score_band(6) == "yellow"
    assert score_band(5.99) == "magenta"  # NOT green — green is exactly 5
    assert score_band(5) == "green"
    assert score_band(4.99) == "magenta"
    assert score_band(1) == "magenta"


# ── The on-demand marker mechanism (mission §6.4) ───────────────────────


@pytest.mark.parity
def test_full_parity_report_walkthrough_against_the_frozen_sample():
    """This is the closest thing this repo has to 'the actual GATE 4
    check' TODAY — and it still spends zero LLM tokens, because there is
    no rebuild P3 output to compare against yet (P3-LLM hasn't landed).
    It runs the real harness end to end against the real frozen sample,
    using the golden fixture as a stand-in for its own rebuild output
    (i.e. demonstrates 'the rebuild reproduced legacy exactly'), and
    prints the human-readable report ADR §9.1 requires the orchestrator
    to review.

    Marked `parity` (see tests/conftest.py) so plain `pytest` / CI never
    selects it — proving the on-demand-exclusion mechanism against a REAL
    test, not just the marker's own registration. Once P3-LLM's real
    output exists, swap `rebuild = golden` below for an actual rebuild
    run over the same frozen sample (see docs/test-strategy.md "Hooking
    in the real rebuild") — the threshold arguments must come from the
    noise-floor measurement (docs/test-strategy.md), never a guess.
    """
    golden = _real_golden_score_rows()
    rebuild = golden  # TODO(P3-LLM): replace with real rebuild output over fixtures/p3_golden_*.json's inputs

    report = check_score_tolerance(
        golden,
        rebuild,
        rank_correlation_threshold=0.8,  # placeholder — see docs/test-strategy.md noise-floor section
        band_agreement_threshold=0.8,  # placeholder — same
    )
    print("\n" + format_tolerance_report(report))
    assert report.n == 100
