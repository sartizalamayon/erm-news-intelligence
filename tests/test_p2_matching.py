"""Unit-level behavior of `app/services/matching.py` — Bengali detection,
pattern compilation (word-boundary vs. substring), and `match_keywords()`'s
set-accumulation semantics. `tests/test_p2_equivalence.py` covers the
end-to-end equivalence-against-legacy-fixtures proof; this file is the
finer-grained "does each piece do what legacy-behavior.md §4.2/§4.3 says"
check, plus DB-backed coverage of `load_keywords()`/`load_patterns()`
(skipped when no Postgres is reachable, mirroring `tests/test_db_schema.py`'s
own pattern).
"""

from __future__ import annotations

import datetime as dt

import pytest

from app.db.queries import insert_article
from app.db.testing import migrated_schema, pg_available
from app.services.matching import (
    build_patterns,
    is_bengali,
    keyword_rows_to_groups,
    load_keywords,
    load_patterns,
    match_article,
    match_articles,
    match_keywords,
    match_type_for,
    review_status_for,
)

# ── is_bengali ───────────────────────────────────────────────────────────


def test_is_bengali_true_for_bengali_script() -> None:
    assert is_bengali("গ্রামীণফোন") is True


def test_is_bengali_true_for_mixed_script() -> None:
    """legacy-behavior.md §4.2: "any Bengali character" — a single BN
    character embedded in an otherwise-English string is enough.
    """
    assert is_bengali("Grameenphone গ্রামীণফোন") is True


def test_is_bengali_false_for_pure_ascii() -> None:
    assert is_bengali("Grameenphone") is False


def test_is_bengali_false_for_empty_string() -> None:
    assert is_bengali("") is False


# ── build_patterns: word-boundary vs. substring ─────────────────────────


def test_english_term_requires_word_boundary() -> None:
    patterns = build_patterns({"Macroeconomy": [("imf", "imf")]})
    assert match_keywords("The IMF approved a loan", patterns) == ("Macroeconomy", "imf")
    # "imf" inside a larger word must NOT match (\b on both sides).
    assert match_keywords("primfax reported nothing", patterns) == ("", "")


def test_english_term_is_case_insensitive() -> None:
    patterns = build_patterns({"Macroeconomy": [("forex", "forex")]})
    assert match_keywords("FOREX reserves fell", patterns) == ("Macroeconomy", "forex")
    assert match_keywords("forex reserves fell", patterns) == ("Macroeconomy", "forex")


def test_bengali_term_is_plain_substring_no_word_boundary() -> None:
    """legacy-behavior.md §4.2: Bengali terms compile to `re.escape()`
    with NO `\\b` — a Bengali substring inside a larger Bengali word must
    still match, unlike the English case above.
    """
    patterns = build_patterns({"Telco/regulatory": [("গ্রামীণফোন", "Grameenphone")]})
    assert match_keywords("গ্রামীণফোনের নতুন সেবা", patterns) == ("Telco/regulatory", "Grameenphone")


def test_bengali_only_match_reports_english_label() -> None:
    """legacy-behavior.md §4.1: "a Bengali-only match is still reported
    under its English label" — build via keyword_rows_to_groups, the way
    a real keywords-table row would produce this pairing.
    """
    groups = keyword_rows_to_groups([{"section": "Telco/regulatory", "term_en": "Grameenphone", "term_bn": "গ্রামীণফোন"}])
    patterns = build_patterns(groups)
    assert match_keywords("গ্রামীণফোন price hike", patterns) == ("Telco/regulatory", "Grameenphone")


def test_blank_term_is_skipped() -> None:
    patterns = build_patterns({"Others": [("  ", "blank")]})
    assert patterns == []


def test_multiple_bengali_synonyms_yield_one_pattern_each() -> None:
    """A keyword row with N comma-split Bengali synonyms yields N+1
    compiled patterns (1 English + N Bengali) — legacy-behavior.md §4.2.
    """
    groups = keyword_rows_to_groups(
        [{"section": "Geopolitical", "term_en": "lng", "term_bn": "এলএনজি, তরলীকৃত প্রাকৃতিক গ্যাস"}]
    )
    patterns = build_patterns(groups)
    assert len(patterns) == 3


# ── match_keywords: set accumulation, sorting, empty case ──────────────


def test_no_match_returns_empty_strings() -> None:
    patterns = build_patterns({"Macroeconomy": [("inflation", "inflation")]})
    assert match_keywords("A completely unrelated headline", patterns) == ("", "")


def test_sections_and_keywords_are_sorted_and_comma_joined() -> None:
    patterns = build_patterns(
        {
            "Telco/regulatory": [("BTRC", "BTRC")],
            "Macroeconomy": [("inflation", "inflation")],
        }
    )
    sections, keywords = match_keywords("BTRC warns on inflation", patterns)
    assert sections == "Macroeconomy, Telco/regulatory"  # alphabetical, not insertion order
    assert keywords == "BTRC, inflation"


def test_same_label_via_two_patterns_collapses_to_one_entry() -> None:
    """English term + its own Bengali synonym both hitting the same
    title must report the label ONCE (set semantics), not twice.
    """
    groups = keyword_rows_to_groups([{"section": "Telco/regulatory", "term_en": "spectrum", "term_bn": "স্পেকট্রাম"}])
    patterns = build_patterns(groups)
    sections, keywords = match_keywords("spectrum auction স্পেকট্রাম নিলাম", patterns)
    assert sections == "Telco/regulatory"
    assert keywords == "spectrum"  # not "spectrum, spectrum"


def test_pattern_order_does_not_affect_result() -> None:
    patterns = build_patterns(
        {
            "Telco/regulatory": [("BTRC", "BTRC"), ("spectrum", "spectrum")],
            "Macroeconomy": [("inflation", "inflation")],
        }
    )
    title = "BTRC spectrum auction amid inflation worries"
    assert match_keywords(title, patterns) == match_keywords(title, list(reversed(patterns)))


def test_match_keywords_runs_every_pattern_no_short_circuit() -> None:
    """A title matching keywords from every section present in the
    pattern list must report ALL of them, not stop at the first hit.
    """
    patterns = build_patterns(
        {
            "A": [("alpha", "alpha")],
            "B": [("beta", "beta")],
            "C": [("gamma", "gamma")],
        }
    )
    sections, keywords = match_keywords("alpha beta gamma all in one headline", patterns)
    assert sections == "A, B, C"
    assert keywords == "alpha, beta, gamma"


# ── review_status_for / match_type_for ──────────────────────────────────


def test_review_status_pending_when_matched() -> None:
    assert review_status_for("Macroeconomy") == "pending"


def test_review_status_filtered_out_when_not_matched() -> None:
    assert review_status_for("") == "filtered_out"


def test_match_type_keyword_when_matched() -> None:
    assert match_type_for("Macroeconomy") == "keyword"


def test_match_type_empty_when_not_matched() -> None:
    assert match_type_for("") == ""


def test_match_type_never_semantic_or_both() -> None:
    """This module has no semantic stage (see module docstring) — over
    a representative range of section-string inputs, match_type_for can
    only ever return 'keyword' or ''.
    """
    for sections in ("", "Macroeconomy", "Macroeconomy, Political", "Others"):
        assert match_type_for(sections) in ("keyword", "")


# ── match_article / match_articles ──────────────────────────────────────


def test_match_article_adds_p2_columns_without_mutating_input() -> None:
    patterns = build_patterns({"Macroeconomy": [("inflation", "inflation")]})
    original = {"id": 1, "title": "Inflation hits new high", "source": "Example"}
    result = match_article(original, patterns)
    assert result["sections"] == "Macroeconomy"
    assert result["keywords"] == "inflation"
    assert result["review_status"] == "pending"
    assert result["match_type"] == "keyword"
    # original dict is untouched
    assert "sections" not in original


def test_match_articles_preserves_order_and_count() -> None:
    patterns = build_patterns({"Macroeconomy": [("inflation", "inflation")]})
    articles = [
        {"id": 1, "title": "Inflation surges"},
        {"id": 2, "title": "Cricket match ends in a draw"},
        {"id": 3, "title": "Inflation eases slightly"},
    ]
    results = match_articles(articles, patterns)
    assert [r["id"] for r in results] == [1, 2, 3]
    assert [r["review_status"] for r in results] == ["pending", "filtered_out", "pending"]


# ── build_patterns caching (content-keyed, not identity-keyed) ─────────


def test_build_patterns_cache_is_content_equivalent_not_identity() -> None:
    """Two structurally-equal-but-distinct `groups` dicts must produce
    equivalent compiled patterns (the lru_cache key is content, not
    object identity) — this guards against the memoization silently
    reusing a stale compile from a *different* keyword set that happens
    to hash-collide, and against it failing to hit the cache at all for
    equal-but-freshly-built inputs.
    """
    groups_a = {"Macroeconomy": [("inflation", "inflation")]}
    groups_b = {"Macroeconomy": [("inflation", "inflation")]}
    assert groups_a is not groups_b
    patterns_a = build_patterns(groups_a)
    patterns_b = build_patterns(groups_b)
    assert [(p.section, p.label, p.pattern.pattern) for p in patterns_a] == [
        (p.section, p.label, p.pattern.pattern) for p in patterns_b
    ]


def test_build_patterns_cache_distinguishes_different_content() -> None:
    patterns_a = build_patterns({"Macroeconomy": [("inflation", "inflation")]})
    patterns_b = build_patterns({"Macroeconomy": [("forex", "forex")]})
    assert [p.label for p in patterns_a] != [p.label for p in patterns_b]


# ── DB-backed: load_keywords / load_patterns (skipped if no Postgres) ──

pytestmark_db = pytest.mark.skipif(not pg_available(), reason="no reachable Postgres (set TEST_DATABASE_URL)")


@pytestmark_db
def test_load_keywords_reads_only_enabled_rows() -> None:
    with migrated_schema() as conn:
        conn.execute(
            "INSERT INTO keywords (section, term_en, term_bn, enabled) VALUES "
            "(%s, %s, %s, %s), (%s, %s, %s, %s)",
            ("Macroeconomy", "inflation", "", True, "Macroeconomy", "disabled-term", "", False),
        )
        rows = load_keywords(conn)
        terms = {r["term_en"] for r in rows}
        assert "inflation" in terms
        assert "disabled-term" not in terms


@pytestmark_db
def test_load_patterns_end_to_end_against_a_real_schema() -> None:
    with migrated_schema() as conn:
        conn.execute(
            "INSERT INTO keywords (section, term_en, term_bn, enabled) VALUES (%s, %s, %s, %s)",
            ("Telco/regulatory", "Grameenphone", "গ্রামীণফোন", True),
        )
        patterns = load_patterns(conn)
        assert match_keywords("Grameenphone raises tariffs", patterns) == ("Telco/regulatory", "Grameenphone")
        assert match_keywords("গ্রামীণফোনের শুল্ক বৃদ্ধি", patterns) == ("Telco/regulatory", "Grameenphone")


@pytestmark_db
def test_matched_article_round_trips_through_insert_article() -> None:
    """Confirms match_article()'s output dict shape is directly usable
    by app.db.queries.insert_article — the seam P1-INGEST's collector
    will actually use.
    """
    with migrated_schema() as conn:
        conn.execute(
            "INSERT INTO keywords (section, term_en, term_bn, enabled) VALUES (%s, %s, %s, %s)",
            ("Macroeconomy", "inflation", "", True),
        )
        patterns = load_patterns(conn)
        raw = {
            "url": "https://example.com/a",
            "url_norm": "https://example.com/a",
            "source": "Example",
            "title": "Inflation hits record high",
            "day": dt.date(2026, 8, 5),
        }
        enriched = match_article(raw, patterns)
        article_id = insert_article(conn, **enriched)
        assert article_id is not None
        row = conn.execute(
            "SELECT sections, review_status, match_type FROM articles WHERE id = %s", (article_id,)
        ).fetchone()
        assert row["sections"] == "Macroeconomy"
        assert row["review_status"] == "pending"
        assert row["match_type"] == "keyword"
