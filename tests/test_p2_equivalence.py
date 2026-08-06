"""GATE 3 for the REAL implementation: `app/services/matching.py` recomputed
against the frozen fixtures must reproduce `fixtures/p2_matched_*.json`
exactly, and the GATE-3 differ (`tests/helpers/differ.py`) must report an
empty diff.

This is deliberately a separate file from `tests/test_p1p2_equivalence.py`
(ARCH-TEST's, outside this task's file-ownership — see mission "FILES YOU
OWN"), which exercises the same fixtures against
`tests.helpers.legacy_reference`'s oracle port. That file is left as-is;
this one is the mission-required proof that the REAL rebuild function
(not the oracle stand-in) agrees with legacy, field for field, with the
diff mechanism run over real fixture data too.
"""

from __future__ import annotations

from app.services.matching import (
    build_patterns,
    keyword_rows_to_groups,
    match_type_for,
    review_status_for,
)
from app.services.matching import match_keywords as real_match_keywords
from tests.fixtures.loaders import load_keywords_snapshot, load_p1_raw, load_p2_matched
from tests.helpers.differ import diff_matched_sets

DETERMINISTIC_FIELDS = ("sections", "keywords", "review_status", "match_type")


def _real_patterns():
    kw_rows = load_keywords_snapshot()["rows"]
    return build_patterns(keyword_rows_to_groups(kw_rows))


def test_real_matching_engine_reproduces_p2_matched_fixture_exactly() -> None:
    """The mission-required equivalence proof, against the REAL
    app.services.matching functions (not the test-helper oracle).
    Reports the exact match count in the assertion message per mission
    DoD #3: "report the exact match count", not "matches".
    """
    patterns = _real_patterns()
    p1_by_id = {row["id"]: row for row in load_p1_raw()["rows"]}
    p2_by_id = {row["id"]: row for row in load_p2_matched()["rows"]}
    assert set(p1_by_id) == set(p2_by_id)

    total_fields = 0
    mismatches = []
    for article_id, p1_row in p1_by_id.items():
        sections, keywords = real_match_keywords(p1_row["title"], patterns)
        recomputed = {
            "sections": sections,
            "keywords": keywords,
            "review_status": review_status_for(sections),
            "match_type": match_type_for(sections),
        }
        recorded = p2_by_id[article_id]
        for field in DETERMINISTIC_FIELDS:
            total_fields += 1
            if recomputed[field] != recorded[field]:
                mismatches.append((article_id, field, recomputed[field], recorded[field]))

    assert not mismatches, (
        f"{len(mismatches)}/{total_fields} field(s) diverged between the real "
        f"app.services.matching output and fixtures/p2_matched: {mismatches[:10]}"
    )
    # Exact-count proof for the report: 100 rows x 4 fields = 400 field
    # comparisons, all agreeing.
    assert total_fields == len(p1_by_id) * len(DETERMINISTIC_FIELDS)


def test_real_matching_engine_exercises_both_matched_and_unmatched_rows() -> None:
    """Guards against the equivalence test above passing vacuously."""
    patterns = _real_patterns()
    outcomes = {bool(real_match_keywords(row["title"], patterns)[0]) for row in load_p1_raw()["rows"]}
    assert outcomes == {True, False}


def test_gate3_diff_against_real_p2_matched_fixture_is_empty() -> None:
    """Runs the real matching engine over fixtures/p1_raw, builds the
    "rebuild" row set (id + match_type, keyed the way diff_matched_sets
    expects), and diffs it against the real legacy fixture's matched
    rows. This fixture's sampled day predates any semantic-recall
    activity (test-strategy.md §1.2 / test_p1p2_equivalence.py's own
    sanity check), so the expected result is a TRUE empty diff — zero
    unexpected AND zero expected-delta rows, not just "no unexpected
    ones because everything got swallowed by the semantic exemption".
    """
    patterns = _real_patterns()
    p2_matched_rows = load_p2_matched()["rows"]
    legacy_matched = [row for row in p2_matched_rows if row["sections"]]

    rebuild_matched = []
    for row in load_p1_raw()["rows"]:
        sections, _keywords = real_match_keywords(row["title"], patterns)
        if sections:
            rebuild_matched.append({"id": row["id"], "match_type": match_type_for(sections)})

    result = diff_matched_sets(legacy_matched, rebuild_matched)
    assert result.is_empty, result.format_report()
    assert result.expected == [], "no semantic-recall rows exist in this fixture; the delta list must stay empty"
    assert len(legacy_matched) == len(rebuild_matched) == 50  # fixture is deliberately balanced 50 matched / 50 not


def test_gate3_diff_would_have_caught_a_real_regression() -> None:
    """Negative control: diff_matched_sets must NOT be a rubber stamp.
    Drop one real legacy-matched row from the "rebuild" side and confirm
    the gate fails loudly, with that row identified.
    """
    legacy_matched = [row for row in load_p2_matched()["rows"] if row["sections"]]
    rebuild_missing_one = [{"id": row["id"], "match_type": row["match_type"]} for row in legacy_matched[1:]]
    result = diff_matched_sets(legacy_matched, rebuild_missing_one)
    assert not result.is_empty
    assert [row["id"] for row in result.unexpected_legacy_only] == [legacy_matched[0]["id"]]
