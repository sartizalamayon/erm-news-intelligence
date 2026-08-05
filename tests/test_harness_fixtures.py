"""Sanity/shape checks on the committed fixture tiers themselves
(fixtures/*.json — see tests/fixtures/build_fixtures.py for how they were
produced). These tests read ONLY the committed JSON via
tests/fixtures/loaders.py, never `data/news.db` — they must pass in CI,
which has no local copy of the corpus.

If any of these ever fail after someone regenerates the fixtures, that's
the signal to also refresh FREEZE_DATE in tests/fixtures/loaders.py and
re-check the numbers hardcoded below (row counts, stratum targets) still
make sense for the new snapshot.
"""

from __future__ import annotations

from tests.fixtures.loaders import (
    load_keywords_snapshot,
    load_p1_raw,
    load_p2_matched,
    load_p3_golden,
)
from tests.helpers.legacy_reference import is_bengali

P1_P2_EXPECTED_ROWS = 100  # 50 matched + 50 unmatched, see build_fixtures.py
P3_GOLDEN_EXPECTED_ROWS = 100


def test_keywords_snapshot_has_all_sections_and_is_all_enabled():
    kw = load_keywords_snapshot()["rows"]
    assert len(kw) > 0
    sections = {row["section"] for row in kw}
    # legacy-behavior.md §4.1's five documented sections.
    assert sections == {"Macroeconomy", "Telco/regulatory", "Geopolitical", "Political", "Others"}
    assert all(row["enabled"] == 1 for row in kw)


def test_p1_raw_matches_documented_output_contract_columns():
    rows = load_p1_raw()["rows"]
    assert len(rows) == P1_P2_EXPECTED_ROWS
    expected_columns = {"id", "url", "url_norm", "source", "title", "published", "day"}
    for row in rows:
        assert set(row) == expected_columns, row
    # P1's contract explicitly has NO sections/keywords/review_status —
    # those are P2's output (legacy-behavior.md §2.6).
    forbidden = {"sections", "keywords", "review_status", "match_type", "llm_score", "content_text"}
    for row in rows:
        assert forbidden.isdisjoint(row), row


def test_p1_raw_all_rows_from_the_same_frozen_day():
    rows = load_p1_raw()["rows"]
    days = {row["day"] for row in rows}
    assert days == {"2026-08-05"}


def test_p1_raw_urls_are_unique():
    rows = load_p1_raw()["rows"]
    urls = [row["url"] for row in rows]
    assert len(urls) == len(set(urls))


def test_p2_matched_is_p1_raw_plus_p2_output_columns():
    p1_ids = {row["id"] for row in load_p1_raw()["rows"]}
    p2_rows = load_p2_matched()["rows"]
    assert len(p2_rows) == P1_P2_EXPECTED_ROWS
    assert {row["id"] for row in p2_rows} == p1_ids
    expected_columns = {
        "id", "url", "url_norm", "source", "title", "published", "day",
        "sections", "keywords", "review_status", "match_type",
    }
    for row in p2_rows:
        assert set(row) == expected_columns, row


def test_p2_matched_is_deliberately_balanced_matched_vs_unmatched():
    """build_fixtures.py oversamples matched rows on purpose (~50/50, not
    the real ~19.5% selectivity) so the P2 equivalence test in
    test_p1p2_equivalence.py exercises both outcomes richly. This asserts
    that design choice held, not that it reflects production selectivity
    (legacy-behavior.md §4.4 already documents real selectivity from the
    live DB — this fixture is not trying to re-derive it).
    """
    rows = load_p2_matched()["rows"]
    matched = sum(1 for r in rows if r["sections"])
    unmatched = len(rows) - matched
    assert matched == 50
    assert unmatched == 50


def test_p2_matched_review_status_and_match_type_are_consistent_with_sections():
    for row in load_p2_matched()["rows"]:
        if row["sections"]:
            assert row["review_status"] == "pending"
            assert row["match_type"] == "keyword"
        else:
            assert row["review_status"] == "filtered_out"
            assert row["match_type"] == ""


def test_p3_golden_has_the_documented_row_count_and_columns():
    rows = load_p3_golden()["rows"]
    assert len(rows) == P3_GOLDEN_EXPECTED_ROWS
    expected_columns = {
        "id", "title", "title_en", "source", "sections", "keywords", "content_text",
        "dup_group", "is_primary", "also_in", "match_type", "semantic_score",
        "llm_score", "llm_reason", "topic_tag", "topic_standout", "topic_parent_id",
        "day", "content_text_truncated", "score_band", "language",
    }
    for row in rows:
        assert set(row) == expected_columns, row


def test_p3_golden_every_row_is_scored_and_keyword_matched():
    """The parity sample is drawn from llm_score IS NOT NULL — every row
    must therefore be a real, scored article, and per legacy-behavior.md
    §5 the semantic-only-promoted rows (0.008% of the corpus) don't even
    have llm_score populated in a way that would land them here by
    accident — confirmed directly: none of match_type == 'semantic'.
    """
    for row in load_p3_golden()["rows"]:
        assert row["llm_score"] is not None
        assert 1.0 <= row["llm_score"] <= 10.0
        assert row["match_type"] in ("keyword", "")  # never 'semantic' or 'both' (legacy-behavior.md §5.5, §7.8)


def test_p3_golden_content_text_is_capped_at_300_chars():
    for row in load_p3_golden()["rows"]:
        assert len(row["content_text"]) <= 300


def test_p3_golden_score_band_and_language_labels_are_self_consistent():
    """The fixture's own score_band/language fields must match what an
    independent recompute from llm_score/title says — these labels are
    build_fixtures.py's own annotations, not something read off the DB,
    so if this fails the generator has a bug, not the corpus.
    """
    for row in load_p3_golden()["rows"]:
        score = row["llm_score"]
        if score >= 8:
            expected_band = "red"
        elif score >= 6:
            expected_band = "yellow"
        elif score == 5:
            expected_band = "green"
        else:
            expected_band = "magenta"
        assert row["score_band"] == expected_band, row

        expected_lang = "bn" if is_bengali(row["title"]) else "en"
        assert row["language"] == expected_lang, row


def test_p3_golden_is_stratified_by_score_band_and_language_per_meta():
    """Mission requirement: 'stratified by score band AND by language,
    since the corpus is 72.4% Bengali and an English-skewed sample would
    make parity meaningless'. Confirms every one of the 8 strata is
    actually represented with more than a token count, and recomputes the
    realized distribution directly from the rows (not just trusting the
    _meta block).
    """
    rows = load_p3_golden()["rows"]
    from collections import Counter

    dist = Counter((row["score_band"], row["language"]) for row in rows)
    assert set(dist) == {
        ("red", "bn"), ("red", "en"),
        ("yellow", "bn"), ("yellow", "en"),
        ("green", "bn"), ("green", "en"),
        ("magenta", "bn"), ("magenta", "en"),
    }
    assert all(n >= 5 for n in dist.values()), dist

    bn_total = sum(n for (_, lang), n in dist.items() if lang == "bn")
    assert 55 <= bn_total <= 75  # ~66% Bengali, well clear of a 50/50 or English-skewed sample


def test_p3_golden_ids_are_disjoint_from_p1_p2_sample():
    """Not a requirement, just documenting reality: the P3 golden sample
    (scored articles, any day) and the P1/P2 sample (one specific day,
    2026-08-05, matched+unmatched) are independently drawn and may or may
    not overlap. This test pins today's actual relationship so a future
    regeneration that silently changes it is visible in a diff.
    """
    p3_ids = {row["id"] for row in load_p3_golden()["rows"]}
    p1_ids = {row["id"] for row in load_p1_raw()["rows"]}
    assert p3_ids.isdisjoint(p1_ids)
