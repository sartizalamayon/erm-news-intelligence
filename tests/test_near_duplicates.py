"""Tests for the lexical near-duplicate pre-cluster
(`app.services.near_duplicates`) — the replacement for P3's permanently
unavailable embedding backend. See that module's header for why the
dedup stage detected zero duplicates on every run before it existed.

The bar these tests hold the clusterer to is deliberately asymmetric:
FALSE MERGES are the dangerous failure (an unrelated story gets folded
under another and disappears from the reviewer's queue), while a missed
merge is a cosmetic duplicate. Several cases below therefore assert
"these must NOT group" rather than only "these must group".
"""

from __future__ import annotations

from app.services.near_duplicates import (
    MAX_GROUP_SIZE,
    propose_duplicate_groups,
    tokenize,
)


def _alloc():
    counter = {"n": 0}

    def allocate() -> int:
        counter["n"] += 1
        return counter["n"]

    return allocate


def _group_of(articles, index):
    return articles[index]["dup_group"]


# ── tokenize ─────────────────────────────────────────────────────────────


def test_tokenize_drops_stopwords_and_single_chars():
    assert tokenize("The price of gas is a") == frozenset({"price", "gas"})


def test_tokenize_is_punctuation_and_case_insensitive():
    assert tokenize("Petrobangla's GAS-crisis!") == tokenize("petrobangla s gas crisis")


def test_tokenize_handles_bengali():
    tokens = tokenize("গ্যাস সংকটে দুঃখ প্রকাশ পেট্রোবাংলার")
    assert "গ্যাস" in tokens
    assert tokens  # not empty — the Bengali block must survive normalization


def test_tokenize_empty_input_is_empty_set():
    assert tokenize("") == frozenset()
    assert tokenize("   ") == frozenset()


# ── grouping ─────────────────────────────────────────────────────────────


def test_groups_reworded_coverage_of_the_same_story():
    articles = [
        {"title_en": "ADB sets over $1b annual pipeline under Bangladesh growth plan"},
        {"title_en": "ADB, Bangladesh plan over $1b annual investments pipeline"},
    ]
    assert propose_duplicate_groups(articles, allocate_id=_alloc()) == 1
    assert _group_of(articles, 0) == _group_of(articles, 1)
    assert _group_of(articles, 0) is not None


def test_does_not_group_unrelated_stories():
    articles = [
        {"title_en": "Cricket board announces new selection panel for home series"},
        {"title_en": "Central bank raises policy rate to tame inflation pressure"},
    ]
    assert propose_duplicate_groups(articles, allocate_id=_alloc()) == 0
    assert _group_of(articles, 0) is None
    assert _group_of(articles, 1) is None


def test_short_headlines_are_never_grouped():
    """Below MIN_TOKENS_FOR_MATCH there is not enough signal — two
    3-word gas headlines about different events would otherwise merge.
    """
    articles = [{"title_en": "Gas crisis deepens"}, {"title_en": "Gas crisis worsens"}]
    assert propose_duplicate_groups(articles, allocate_id=_alloc()) == 0
    assert _group_of(articles, 0) is None


def test_containment_matches_short_and_long_variants():
    articles = [
        {"title_en": "Petrobangla apologises for temporary gas supply disruption in Dhaka"},
        {"title_en": "Petrobangla apologises for temporary gas supply disruption in Dhaka after nationwide outage"},
    ]
    assert propose_duplicate_groups(articles, allocate_id=_alloc()) == 1
    assert _group_of(articles, 0) == _group_of(articles, 1)


def test_prefers_title_en_over_original_title():
    """Dedup runs after translate; comparing a Bengali original against
    an already-English headline would never match on tokens.
    """
    articles = [
        {"title": "সম্পূর্ণ ভিন্ন বাংলা শিরোনাম", "title_en": "Central bank raises policy rate to tame inflation"},
        {"title": "আরেকটি ভিন্ন শিরোনাম", "title_en": "Central bank raises the policy rate to tame inflation"},
    ]
    assert propose_duplicate_groups(articles, allocate_id=_alloc()) == 1
    assert _group_of(articles, 0) == _group_of(articles, 1)


def test_falls_back_to_title_when_no_translation():
    articles = [
        {"title": "Central bank raises policy rate to tame inflation pressure"},
        {"title": "Central bank raises the policy rate to tame inflation pressure", "title_en": ""},
    ]
    assert propose_duplicate_groups(articles, allocate_id=_alloc()) == 1


def test_transitive_clustering_merges_a_chain():
    a = "Government approves new power plant project in Chattogram district"
    b = "Government approves new power plant project in Chattogram"
    c = "Government approves the new power plant project in Chattogram district today"
    articles = [{"title_en": a}, {"title_en": b}, {"title_en": c}]
    assert propose_duplicate_groups(articles, allocate_id=_alloc()) == 1
    assert _group_of(articles, 0) == _group_of(articles, 1) == _group_of(articles, 2)


def test_oversized_group_is_discarded_not_emitted():
    """A runaway transitive chain is a thresholds failure, not a real
    story with 40+ outlets. It must be dropped (and logged), never
    handed downstream as a legitimate cluster.
    """
    base = "Government approves new power plant project in Chattogram district number"
    articles = [{"title_en": f"{base} {i}"} for i in range(MAX_GROUP_SIZE + 5)]
    assert propose_duplicate_groups(articles, allocate_id=_alloc()) == 0
    assert all(a["dup_group"] is None for a in articles)


def test_ids_come_from_the_allocator_not_a_local_counter():
    """Cluster ids must be globally unique (migrations/0017): review.py
    reads them back with an unscoped `WHERE dup_group = ANY(...)`, so a
    per-run counter would alias groups across days.
    """
    allocator_values = iter([500, 501, 502])
    articles = [
        {"title_en": "Central bank raises policy rate to tame inflation pressure"},
        {"title_en": "Central bank raises the policy rate to tame inflation pressure"},
    ]
    propose_duplicate_groups(articles, allocate_id=lambda: next(allocator_values))
    assert _group_of(articles, 0) == 500


def test_empty_and_single_article_inputs_are_safe():
    assert propose_duplicate_groups([], allocate_id=_alloc()) == 0
    one = [{"title_en": "Only one story here today"}]
    assert propose_duplicate_groups(one, allocate_id=_alloc()) == 0
    assert one[0]["dup_group"] is None


def test_missing_and_blank_titles_do_not_crash_or_group():
    articles = [{"title_en": ""}, {"title": ""}, {}, {"title_en": "   "}]
    assert propose_duplicate_groups(articles, allocate_id=_alloc()) == 0
    assert all(a["dup_group"] is None for a in articles)


def test_rerun_reassigns_rather_than_preserving_stale_ids():
    articles = [
        {"title_en": "A totally standalone story about the weather today", "dup_group": 99},
        {"title_en": "Another standalone story about tomorrow's cricket match", "dup_group": 99},
    ]
    propose_duplicate_groups(articles, allocate_id=_alloc())
    assert _group_of(articles, 0) is None
    assert _group_of(articles, 1) is None
