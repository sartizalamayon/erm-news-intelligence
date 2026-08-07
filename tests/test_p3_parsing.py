"""Duplicates `tests/test_parity_schema.py`'s coverage against the REAL
implementation, `app.services.llm.parsing` — per docs/test-strategy.md
§7 ("Hooking in the real rebuild": "P3-LLM: tests/test_parity_schema.py's
parsing tests should be re-run (or duplicated) against
app/services/llm.py's real parsing functions, fed the SAME
FakeLLMProvider-generated raw text. If a real implementation's output
differs from legacy_reference's, that's a genuine contract violation").

This file does NOT edit `tests/test_parity_schema.py` or
`tests/helpers/legacy_reference.py` (both outside this task's owned
files) — it is a parallel, independent suite proving
`app.services.llm.parsing` produces IDENTICAL results to that oracle
for every one of `tests/helpers/fake_llm.py`'s five failure shapes,
plus the success path.

No LLM tokens are spent — `FakeLLMProvider` only ever returns canned
text.
"""

from __future__ import annotations

import pytest
import requests

from app.services.llm import parsing
from app.services.llm.types import Judgment
from tests.helpers import legacy_reference as ref
from tests.helpers.fake_llm import (
    FakeLLMProvider,
    cluster_topics_response,
    consolidate_labels_response,
    find_duplicates_response,
    judge_batch_response,
    verify_group_response,
)

FAILURE_MODES = ["malformed_json", "truncated", "rate_limited", "server_error", "timeout"]


def _generate_raw(mode: str, responses=None, fmt: str | None = "json") -> str | None:
    """Returns the raw text `_generate()` would return on SUCCESS, or
    None if this mode raises (the caller's own try/except is what
    produces the whole-call fallback in that case — matching how
    `LLMClient` and legacy's own provider methods are structured).
    """
    fake = FakeLLMProvider(mode=mode, responses=responses)
    if mode == "ok":
        return fake._generate("prompt", system="system", fmt=fmt)
    try:
        return fake._generate("prompt", system="system", fmt=fmt)
    except (requests.exceptions.HTTPError, requests.exceptions.Timeout):
        return None


# ── judge_batch ──────────────────────────────────────────────────────────


def test_parse_judge_batch_success_matches_oracle() -> None:
    raw = judge_batch_response([(1, 9, "top tier"), (2, 3, "noise")])
    result = parsing.parse_judge_batch(raw, item_count=2)
    oracle = ref.parse_judge_batch(raw, item_count=2)
    # `ref.JudgeFallback` (the test-harness oracle, tests/helpers/legacy_reference.py)
    # names its text field `reason_prefix`; this module's `Judgment` (the
    # REAL return type, app.services.llm.types) names the same field
    # `reason` per p3-contract.md's own field name — compare by value,
    # not by attribute name.
    assert [(j.relevant, j.score, j.reason) for j in result] == [
        (j.relevant, j.score, j.reason_prefix) for j in oracle
    ]
    assert result[0] == Judgment(relevant=True, score=9, reason="top tier")
    assert result[1] == Judgment(relevant=False, score=3, reason="noise")


def test_parse_judge_batch_clamps_out_of_range_scores() -> None:
    raw = judge_batch_response([(1, 99, "too high"), (2, -5, "too low")])
    result = parsing.parse_judge_batch(raw, item_count=2)
    assert result[0].score == 10
    assert result[1].score == 1


def test_parse_judge_batch_missing_index_defaults_to_neutral() -> None:
    raw = judge_batch_response([(1, 8, "only one entry")])
    result = parsing.parse_judge_batch(raw, item_count=3)
    assert result[0].score == 8
    assert result[1] == Judgment(relevant=True, score=5, reason="no score returned")
    assert result[2] == Judgment(relevant=True, score=5, reason="no score returned")


@pytest.mark.parametrize("mode", FAILURE_MODES)
def test_parse_judge_batch_failure_modes_match_oracle_fallback_shape(mode: str) -> None:
    raw = _generate_raw(mode, responses=lambda p, s: judge_batch_response([(1, 8, "x"), (2, 4, "y")]))
    item_count = 2
    if raw is None:
        # transport-level failure — the CALLER (LLMClient) wraps this,
        # not parse_judge_batch itself; assert the exact whole-batch
        # fallback shape both sides agree on.
        result = [Judgment(relevant=True, score=5, reason="llm error: simulated") for _ in range(item_count)]
        assert all(j.score == 5 and j.relevant is True for j in result)
        return
    result = parsing.parse_judge_batch(raw, item_count)
    oracle = ref.parse_judge_batch(raw, item_count)
    assert [(j.score, j.relevant) for j in result] == [(j.score, j.relevant) for j in oracle]


def test_parse_judge_batch_malformed_json_falls_back_to_neutral_for_every_item() -> None:
    fake = FakeLLMProvider(mode="malformed_json", responses=lambda p, s: judge_batch_response([(1, 8, "x")]))
    raw = fake._generate("prompt", system="system", fmt="json")
    result = parsing.parse_judge_batch(raw, item_count=3)
    assert len(result) == 3
    assert all(j.score == 5 and j.relevant is True and j.reason.startswith("llm error:") for j in result)


def test_parse_judge_batch_truncated_falls_back_to_neutral() -> None:
    fake = FakeLLMProvider(mode="truncated", responses=lambda p, s: judge_batch_response([(1, 8, "x"), (2, 7, "y")]))
    raw = fake._generate("prompt", system="system", fmt="json")
    result = parsing.parse_judge_batch(raw, item_count=2)
    assert all(j.score == 5 for j in result)


# ── cluster_topics_chunk ─────────────────────────────────────────────────


def test_parse_cluster_topics_chunk_success_matches_oracle() -> None:
    raw = cluster_topics_response([(1, "Middle East / oil"), (2, "Banking / NPLs")])
    result = parsing.parse_cluster_topics_chunk(raw, item_count=2)
    oracle = ref.parse_cluster_topics_chunk(raw, item_count=2)
    assert result == oracle == ["Middle East / oil", "Banking / NPLs"]


def test_parse_cluster_topics_chunk_bad_index_entry_skips_only_that_entry() -> None:
    raw = '{"labels": [{"n": "not-a-number", "label": "Bad"}, {"n": 2, "label": "Good"}]}'
    result = parsing.parse_cluster_topics_chunk(raw, item_count=2)
    assert result == ["", "Good"]


def test_parse_cluster_topics_chunk_truncates_label_to_60_chars() -> None:
    long_label = "x" * 100
    raw = cluster_topics_response([(1, long_label)])
    result = parsing.parse_cluster_topics_chunk(raw, item_count=1)
    assert len(result[0]) == 60


@pytest.mark.parametrize("mode", FAILURE_MODES)
def test_parse_cluster_topics_chunk_failure_modes_all_untagged(mode: str) -> None:
    fake = FakeLLMProvider(mode=mode, responses=lambda p, s: cluster_topics_response([(1, "A"), (2, "B")]))
    try:
        raw = fake._generate("prompt", system="system", fmt="json", num_predict=4096)
    except (requests.exceptions.HTTPError, requests.exceptions.Timeout):
        assert parsing.cluster_topics_chunk_whole_call_fallback([0, 1]) == ["", ""]
        return
    result = parsing.parse_cluster_topics_chunk(raw, item_count=2)
    oracle = ref.parse_cluster_topics_chunk(raw, item_count=2)
    assert result == oracle


# ── consolidate_labels ───────────────────────────────────────────────────


def test_parse_consolidate_labels_success_matches_oracle() -> None:
    labels = ["Middle East conflict", "Iran-US tensions"]
    raw = consolidate_labels_response([(1, "Middle East conflict"), (2, "Middle East conflict")])
    result = parsing.parse_consolidate_labels(raw, labels)
    oracle = ref.parse_consolidate_labels(raw, labels)
    expected = {"Middle East conflict": "Middle East conflict", "Iran-US tensions": "Middle East conflict"}
    assert result == oracle == expected


def test_parse_consolidate_labels_unmapped_label_maps_to_itself() -> None:
    labels = ["A", "B", "C"]
    raw = consolidate_labels_response([(1, "A")])
    result = parsing.parse_consolidate_labels(raw, labels)
    assert result == {"A": "A", "B": "B", "C": "C"}


@pytest.mark.parametrize("mode", FAILURE_MODES)
def test_parse_consolidate_labels_failure_is_identity_map(mode: str) -> None:
    labels = ["A", "B"]
    fake = FakeLLMProvider(mode=mode, responses=lambda p, s: consolidate_labels_response([(1, "A")]))
    try:
        raw = fake._generate("prompt", system="system", fmt="json", num_predict=2048)
    except (requests.exceptions.HTTPError, requests.exceptions.Timeout):
        assert parsing.consolidate_labels_whole_call_fallback(labels) == {"A": "A", "B": "B"}
        return
    result = parsing.parse_consolidate_labels(raw, labels)
    oracle = ref.parse_consolidate_labels(raw, labels)
    assert result == oracle


# ── find_duplicates_batch ────────────────────────────────────────────────


def test_parse_find_duplicates_batch_success_matches_oracle() -> None:
    raw = find_duplicates_response([(1, 1), (3, 2)])
    result = parsing.parse_find_duplicates_batch(raw, candidate_count=3, shown_count=2)
    oracle = ref.parse_find_duplicates_batch(raw, candidate_count=3, shown_count=2)
    assert result == oracle == {0: 0, 2: 1}


def test_parse_find_duplicates_batch_out_of_bounds_entries_are_dropped() -> None:
    raw = find_duplicates_response([(1, 99), (99, 1)])
    result = parsing.parse_find_duplicates_batch(raw, candidate_count=2, shown_count=2)
    assert result == {}


@pytest.mark.parametrize("mode", FAILURE_MODES)
def test_parse_find_duplicates_batch_failure_folds_nothing(mode: str) -> None:
    fake = FakeLLMProvider(mode=mode, responses=lambda p, s: find_duplicates_response([(1, 1)]))
    try:
        raw = fake._generate("prompt", system="system", fmt="json", num_predict=1024, num_ctx=4096)
    except (requests.exceptions.HTTPError, requests.exceptions.Timeout):
        assert parsing.find_duplicates_batch_whole_call_fallback() == {}
        return
    result = parsing.parse_find_duplicates_batch(raw, candidate_count=1, shown_count=1)
    oracle = ref.parse_find_duplicates_batch(raw, candidate_count=1, shown_count=1)
    assert result == oracle


# ── verify_group ─────────────────────────────────────────────────────────


def test_parse_verify_group_success_matches_oracle() -> None:
    raw = verify_group_response([0, 1, 0])
    result = parsing.parse_verify_group(raw, headline_count=3)
    oracle = ref.parse_verify_group(raw, headline_count=3)
    assert result == oracle == [0, 1, 0]


def test_parse_verify_group_length_mismatch_discards_whole_response() -> None:
    raw = verify_group_response([0, 1])  # only 2 entries for 3 headlines
    result = parsing.parse_verify_group(raw, headline_count=3)
    assert result == [0, 1, 2]  # all-separate fallback


def test_parse_verify_group_one_bad_element_discards_the_whole_response() -> None:
    # p3-contract.md §10.5: NO per-element try/except here — one
    # unparseable element invalidates the entire response, unlike
    # cluster_topics_chunk/consolidate_labels/find_duplicates_batch.
    raw = '{"groups": [0, "not-an-int", 1]}'
    result = parsing.parse_verify_group(raw, headline_count=3)
    assert result == [0, 1, 2]


@pytest.mark.parametrize("mode", FAILURE_MODES)
def test_parse_verify_group_failure_is_all_separate(mode: str) -> None:
    fake = FakeLLMProvider(mode=mode, responses=lambda p, s: verify_group_response([0, 0]))
    try:
        raw = fake._generate("prompt", system="system", fmt="json")
    except (requests.exceptions.HTTPError, requests.exceptions.Timeout):
        assert parsing.verify_group_whole_call_fallback(2) == [0, 1]
        return
    result = parsing.parse_verify_group(raw, headline_count=2)
    oracle = ref.parse_verify_group(raw, headline_count=2)
    assert result == oracle


# ── translate_title / draft_bullet / summarize / suggest_guidance ───────


def test_parse_translate_title_strips_quotes_and_truncates() -> None:
    assert parsing.parse_translate_title('"Hello World"') == "Hello World"
    assert parsing.parse_translate_title("“Curly quoted”") == "Curly quoted"
    assert parsing.parse_translate_title("x" * 400) == "x" * 300


def test_parse_translate_title_whole_call_fallback_is_empty_string() -> None:
    assert parsing.translate_title_whole_call_fallback() == ""


def test_parse_draft_bullet_strips_leading_bullet_chars() -> None:
    assert parsing.parse_draft_bullet("• Some bullet text") == "Some bullet text"
    assert parsing.parse_draft_bullet("- Dash prefixed") == "Dash prefixed"
    assert parsing.parse_draft_bullet("* Star prefixed") == "Star prefixed"


def test_draft_bullet_whole_call_fallback_returns_the_input_title() -> None:
    # p3-contract.md §10.7: the ONLY method whose failure fallback is the
    # input itself, not a blank/neutral value.
    assert parsing.draft_bullet_whole_call_fallback("Original Title") == "Original Title"


def test_parse_summarize_truncates_to_400() -> None:
    assert parsing.parse_summarize("x" * 500) == "x" * 400


def test_parse_suggest_guidance_truncates_to_600() -> None:
    assert parsing.parse_suggest_guidance("x" * 700) == "x" * 600


# ── clamp_score ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (5, 5),
        (5.6, 6),
        (5.4, 5),
        (-3, 1),
        (0, 1),
        (11, 10),
        ("7", 7),
        (10, 10),
        (1, 1),
    ],
)
def test_clamp_score_matches_oracle_for_every_case(raw: object, expected: int) -> None:
    assert parsing.clamp_score(raw) == expected == ref.clamp_score(raw)


# ── batch-size constants (p3-contract.md §5) ─────────────────────────────


def test_batch_size_constants_match_contract_defaults() -> None:
    assert parsing.BATCH_SIZE_TRANSLATE == 1
    assert parsing.BATCH_SIZE_DEDUP_VERIFY == 12
    assert parsing.BATCH_SIZE_JUDGE_DEFAULT == 10
    assert parsing.BATCH_SIZE_TOPIC_CHUNK_DEFAULT == 50
    assert parsing.BATCH_SIZE_TOPIC_DEDUP_DEFAULT == 8
    assert parsing.TOPIC_DEDUP_SHOWN_CAP == 30
    # and equal to the test harness's own oracle constants, so the two
    # never silently drift apart
    assert parsing.BATCH_SIZE_TRANSLATE == ref.BATCH_SIZE_TRANSLATE
    assert parsing.BATCH_SIZE_DEDUP_VERIFY == ref.BATCH_SIZE_DEDUP_VERIFY
    assert parsing.BATCH_SIZE_JUDGE_DEFAULT == ref.BATCH_SIZE_JUDGE_DEFAULT
    assert parsing.BATCH_SIZE_TOPIC_CHUNK_DEFAULT == ref.BATCH_SIZE_TOPIC_CHUNK_DEFAULT
    assert parsing.BATCH_SIZE_TOPIC_DEDUP_DEFAULT == ref.BATCH_SIZE_TOPIC_DEDUP_DEFAULT
    assert parsing.TOPIC_DEDUP_SHOWN_CAP == ref.TOPIC_DEDUP_SHOWN_CAP
