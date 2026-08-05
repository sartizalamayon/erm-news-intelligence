"""Mission item #6: the mechanism by which GATE 3's "diff against legacy
output = empty" is actually computed.

Two things are tested here:

1. A REAL equivalence check, today, with no rebuild code required: P2's
   entire output (`sections`, `keywords`) is deterministic regex matching
   (legacy-behavior.md §4), so `tests/helpers/legacy_reference.py`'s
   ported `build_patterns()`/`match_keywords()` recomputed from
   `fixtures/p1_raw_*.json` + `fixtures/keywords_snapshot_*.json` must
   reproduce `fixtures/p2_matched_*.json` EXACTLY — this is a genuine,
   passing, non-trivial test of the ported reference logic against real
   historical output, not a tautology (if the port were wrong, this
   fails).

2. The `differ` module's expected-delta mechanism (tests/helpers/differ.py),
   exercised with synthetic legacy/rebuild datasets standing in for what
   GATE 3 will actually compare once the rebuild exists — including the
   documented semantic-recall delta, so that specific known difference is
   proven to be classified as "expected" rather than a failure, and any
   OTHER unexplained difference still fails loudly.
"""

from __future__ import annotations

from tests.fixtures.loaders import load_keywords_snapshot, load_p1_raw, load_p2_matched
from tests.helpers.differ import SEMANTIC_RECALL_REMOVED, diff_matched_sets
from tests.helpers.legacy_reference import (
    build_patterns,
    match_keywords,
    match_type_for,
    patterns_from_keyword_rows,
    review_status_for,
)

# ── 1. Real P2 recompute vs. the frozen fixture ─────────────────────────


def _compiled_patterns():
    kw_rows = load_keywords_snapshot()["rows"]
    groups = patterns_from_keyword_rows(kw_rows)
    return build_patterns(groups)


def test_recomputed_p2_output_matches_the_frozen_fixture_exactly():
    patterns = _compiled_patterns()
    p1_by_id = {row["id"]: row for row in load_p1_raw()["rows"]}
    p2_by_id = {row["id"]: row for row in load_p2_matched()["rows"]}

    assert set(p1_by_id) == set(p2_by_id)

    mismatches = []
    for article_id, p1_row in p1_by_id.items():
        sections, keywords = match_keywords(p1_row["title"], patterns)
        recomputed = {
            "sections": sections,
            "keywords": keywords,
            "review_status": review_status_for(sections),
            "match_type": match_type_for(sections),
        }
        recorded = p2_by_id[article_id]
        for field in ("sections", "keywords", "review_status", "match_type"):
            if recomputed[field] != recorded[field]:
                mismatches.append((article_id, field, recomputed[field], recorded[field]))

    assert not mismatches, (
        f"{len(mismatches)} field(s) diverged between recomputed and recorded P2 output: {mismatches[:10]}"
    )


def test_recompute_actually_exercises_both_matched_and_unmatched_paths():
    """Guards against the previous test passing vacuously (e.g. every
    title trivially matching or trivially not matching, which would make
    the equivalence check meaningless).
    """
    patterns = _compiled_patterns()
    p1_rows = load_p1_raw()["rows"]
    outcomes = {bool(match_keywords(row["title"], patterns)[0]) for row in p1_rows}
    assert outcomes == {True, False}


def test_match_keywords_is_order_independent_and_deduplicating():
    """collector_logic.match_keywords() accumulates into SETS (legacy-
    behavior.md §4.3) — a title matching the same label via two different
    patterns (e.g. English term + its own Bengali synonym) reports that
    label once, not twice, and pattern order doesn't affect the result.
    """
    patterns = _compiled_patterns()
    reversed_patterns = list(reversed(patterns))
    for row in load_p1_raw()["rows"][:25]:
        forward = match_keywords(row["title"], patterns)
        backward = match_keywords(row["title"], reversed_patterns)
        assert forward == backward


# ── 2. GATE 3 empty-diff mechanism, with the known expected delta ──────


def test_expected_delta_absorbs_the_documented_semantic_recall_rows():
    legacy = [
        {"id": 1, "match_type": "keyword"},
        {"id": 2, "match_type": "keyword"},
        {"id": 3, "match_type": "semantic"},  # legacy-only: semantic recall promoted it
        {"id": 4, "match_type": "semantic"},
    ]
    rebuild = [  # rebuild has no semantic stage — 3 and 4 never appear
        {"id": 1, "match_type": "keyword"},
        {"id": 2, "match_type": "keyword"},
    ]
    result = diff_matched_sets(legacy, rebuild)
    assert result.is_empty, result.format_report()
    assert {row["id"] for row, _reason in result.expected} == {3, 4}
    assert result.unexpected_legacy_only == []
    assert result.unexpected_rebuild_only == []


def test_undocumented_legacy_only_row_fails_the_gate():
    """The differ must NOT blanket-forgive every legacy-only row — only
    ones matching a registered ExpectedDelta predicate. A row that's
    legacy-only for some OTHER, unexplained reason must still fail.
    """
    legacy = [
        {"id": 1, "match_type": "keyword"},
        {"id": 2, "match_type": "keyword"},  # rebuild is missing this — no exemption applies
    ]
    rebuild = [{"id": 1, "match_type": "keyword"}]
    result = diff_matched_sets(legacy, rebuild)
    assert not result.is_empty
    assert [row["id"] for row in result.unexpected_legacy_only] == [2]
    assert result.expected == []


def test_rebuild_only_row_always_fails_the_gate_today():
    """No ExpectedDelta exists for "rebuild matched something legacy
    didn't" — the mission only documents a legacy-side delta (semantic
    recall). Any rebuild-only row is unexpected by construction.
    """
    legacy = [{"id": 1, "match_type": "keyword"}]
    rebuild = [{"id": 1, "match_type": "keyword"}, {"id": 99, "match_type": "keyword"}]
    result = diff_matched_sets(legacy, rebuild)
    assert not result.is_empty
    assert [row["id"] for row in result.unexpected_rebuild_only] == [99]


def test_expected_delta_predicate_is_specific_to_semantic_not_a_blanket_pass():
    """Confirms SEMANTIC_RECALL_REMOVED only fires on match_type=='semantic'
    — a row that happens to be legacy-only for a different reason, even if
    superficially similar, is not silently absorbed.
    """
    assert SEMANTIC_RECALL_REMOVED.predicate({"match_type": "semantic"}) is True
    assert SEMANTIC_RECALL_REMOVED.predicate({"match_type": "keyword"}) is False
    assert SEMANTIC_RECALL_REMOVED.predicate({"match_type": ""}) is False
    assert SEMANTIC_RECALL_REMOVED.predicate({}) is False


def test_diff_against_the_real_frozen_p2_sample_with_no_rebuild_output_is_never_falsely_empty():
    """Sanity check against REAL data: if "rebuild output" is simply
    empty (nothing implemented yet), the diff must NOT be empty — every
    matched legacy row is legitimately missing, and none of them are
    semantic (this fixture's day predates full semantic-stage activity /
    contains no semantic rows — see test_harness_fixtures.py), so this
    must fail loudly, not vacuously pass.
    """
    legacy_matched = [row for row in load_p2_matched()["rows"] if row["sections"]]
    result = diff_matched_sets(legacy_matched, rebuild_rows=[])
    assert not result.is_empty
    assert len(result.unexpected_legacy_only) == len(legacy_matched)
