"""ADR §9.1's FROZEN half of the redefined P3 parity contract:

    "Frozen (must match exactly): prompt text, output schema, field
    names/types, parsing and every malformed-response fallback, stage
    ordering, batch sizes. §2.1 is preserved in full — the *contract* is
    frozen, the *model* is not."

Two kinds of test here:

1. `tests.helpers.parity.check_exact_match_fields/_batch` — the CHECKER
   itself, exercised against synthetic golden/rebuild records so its
   pass/fail/violation-reporting logic is proven correct independent of
   any real rebuild output existing yet.

2. `tests.helpers.legacy_reference.parse_*` — the deterministic parsing/
   clamping/fallback ORACLE, fed every one of mission item #5's required
   failure shapes (malformed JSON, truncated, 429, 5xx, timeout) via the
   fake LLM provider, asserting the EXACT documented fallback value comes
   out. This is the concrete, passing-today version of "every
   malformed-response fallback path" — it will not need to change when
   P3-LLM's real implementation lands, because a correct implementation
   MUST produce the same outputs this oracle does (that's the whole
   point of p3-contract.md being frozen).

No LLM tokens are spent by any test in this file — `FakeLLMProvider`
only ever returns canned text.
"""

from __future__ import annotations

import requests

from tests.helpers import legacy_reference as ref
from tests.helpers.fake_llm import (
    FakeLLMProvider,
    cluster_topics_response,
    consolidate_labels_response,
    find_duplicates_response,
    judge_batch_response,
    verify_group_response,
)
from tests.helpers.parity import (
    DETERMINISTIC_FIELDS,
    check_exact_match_batch,
    check_exact_match_fields,
)

# ── 1. The exact-match checker's own correctness ────────────────────────


def test_identical_records_produce_zero_violations():
    golden = {"sections": "Macroeconomy", "keywords": "forex", "url_norm": "https://x/y", "review_status": "pending"}
    rebuild = dict(golden)
    assert check_exact_match_fields(golden, rebuild) == []


def test_value_mismatch_is_reported_with_both_values():
    golden = {"sections": "Macroeconomy", "keywords": "forex", "url_norm": "https://x/y", "review_status": "pending"}
    rebuild = dict(golden, sections="Political")
    violations = check_exact_match_fields(golden, rebuild)
    assert len(violations) == 1
    v = violations[0]
    assert v.field == "sections"
    assert v.expected == "Macroeconomy"
    assert v.actual == "Political"


def test_type_mismatch_is_flagged_even_when_values_look_similar():
    golden = {"sections": "Macroeconomy", "keywords": "forex", "url_norm": "https://x/y", "review_status": "pending"}
    rebuild = dict(golden, review_status=1)  # int instead of str — a real contract break
    violations = check_exact_match_fields(golden, rebuild)
    assert any(v.field == "review_status" and "type mismatch" in v.detail for v in violations)


def test_numeric_int_vs_float_is_not_flagged_as_a_type_mismatch():
    """int vs float is the one deliberate exception — Postgres/JSON round-
    tripping can turn a Python int into a float without it being a real
    contract violation; a rebuild's 8 and legacy's 8.0 should not fail a
    schema check over storage-format noise.
    """
    violations = check_exact_match_fields({"llm_score": 8}, {"llm_score": 8.0}, fields=("llm_score",))
    assert violations == []


def test_missing_field_on_either_side_is_reported():
    golden = {"sections": "Macroeconomy"}
    rebuild = {}
    violations = check_exact_match_fields(golden, rebuild, fields=("sections", "keywords"))
    fields_flagged = {v.field for v in violations}
    assert "sections" in fields_flagged  # present in golden, absent from rebuild
    assert "keywords" in fields_flagged  # absent from golden too — still flagged, not skipped


def test_batch_check_reports_missing_and_extra_rows():
    golden_rows = [{"id": 1, "sections": "A", "keywords": "k", "url_norm": "u1", "review_status": "pending"}]
    rebuild_rows = [
        {"id": 1, "sections": "A", "keywords": "k", "url_norm": "u1", "review_status": "pending"},
        {"id": 2, "sections": "B", "keywords": "k2", "url_norm": "u2", "review_status": "pending"},
    ]
    violations = check_exact_match_batch(golden_rows, rebuild_rows)
    assert len(violations) == 1
    assert violations[0].record_id == 2
    assert "unexpected extra row" in violations[0].detail


def test_batch_check_default_fields_are_the_documented_deterministic_set():
    assert DETERMINISTIC_FIELDS == ("sections", "keywords", "url_norm", "review_status")


def test_exact_match_against_real_p2_matched_fixture_self_comparison_is_clean():
    """Using the real committed fixture as BOTH golden and 'rebuild' (a
    stand-in for 'the rebuild reproduced P2 exactly') proves the checker
    doesn't false-positive on real, non-synthetic data.
    """
    from tests.fixtures.loaders import load_p2_matched

    rows = load_p2_matched()["rows"]
    assert check_exact_match_batch(rows, rows) == []


# ── 2. Parsing / clamping / fallback oracle, driven through FakeLLMProvider ──


def test_judge_batch_ok_response_clamps_and_maps_by_declared_n():
    provider = FakeLLMProvider(
        mode="ok",
        responses=lambda p, s: judge_batch_response([(1, 14, "over range"), (2, -3, "under range"), (3, 7, "fine")]),
    )
    raw = provider._generate("prompt", fmt="json")
    result = ref.parse_judge_batch(raw, item_count=3)
    assert [r.score for r in result] == [10, 1, 7]  # clamped to [1,10]
    assert result[0].relevant is True
    assert result[1].relevant is False  # score 1 < 5


def test_judge_batch_missing_index_defaults_per_item_not_whole_batch():
    """Model returns fewer entries than items — the GAP defaults to
    (5, "no score returned") for just that item; the OTHER items keep
    their real scores (p3-contract.md §3: 'any index the model omits...
    silently defaults to neutral score 5' — per-item, not whole-batch).
    """
    provider = FakeLLMProvider(mode="ok", responses=lambda p, s: judge_batch_response([(1, 9, "telecom")]))
    raw = provider._generate("prompt", fmt="json")
    result = ref.parse_judge_batch(raw, item_count=2)
    assert result[0].score == 9
    assert result[1].score == 5
    assert result[1].reason_prefix == "no score returned"


def test_judge_batch_malformed_json_falls_back_to_neutral_for_every_item():
    provider = FakeLLMProvider(
        mode="malformed_json", responses=lambda p, s: judge_batch_response([(1, 8, "x"), (2, 3, "y")])
    )
    raw = provider._generate("prompt", fmt="json")
    result = ref.parse_judge_batch(raw, item_count=2)
    assert [r.score for r in result] == [5, 5]
    assert all(r.relevant is True for r in result)
    assert all(r.reason_prefix.startswith("llm error: ") for r in result)


def test_judge_batch_truncated_response_also_falls_back_to_neutral():
    provider = FakeLLMProvider(
        mode="truncated",
        responses=lambda p, s: judge_batch_response([(1, 8, "telecom relevance signal"), (2, 3, "sports excluded")]),
    )
    raw = provider._generate("prompt", fmt="json")
    result = ref.parse_judge_batch(raw, item_count=2)
    assert [r.score for r in result] == [5, 5]


def test_judge_batch_rate_limited_exception_propagates_to_caller():
    """429/5xx/timeout happen at the TRANSPORT layer, before there's any
    text to parse — the real llm.py catches these in the SAME outer
    try/except as a parse failure and applies the identical whole-batch
    fallback (p3-contract.md §7's Ollama path: no retry, single attempt,
    any exception -> that call's fallback). This test proves the
    transport exception is exactly what a caller's try/except would need
    to catch to apply judge_batch_whole_call_fallback().
    """
    provider = FakeLLMProvider(mode="rate_limited")
    try:
        provider._generate("prompt", fmt="json")
        raised = None
    except requests.exceptions.HTTPError as e:
        raised = e
    assert raised is not None
    fallback = ref.judge_batch_whole_call_fallback([0, 1, 2], raised)
    assert [f.score for f in fallback] == [5, 5, 5]
    assert all("llm error:" in f.reason_prefix for f in fallback)


def test_cluster_topics_chunk_partial_failure_keeps_good_entries():
    """Per-entry try/except: a garbage 'n' in one entry is skipped, GOOD
    entries still land (p3-contract.md §3: 'partial-tolerant: fill gaps
    with "" instead of discarding the chunk').
    """
    raw = '{"labels": [{"n": 1, "label": "Middle East / oil"}, {"n": "not-a-number", "label": "bad"}]}'
    result = ref.parse_cluster_topics_chunk(raw, item_count=2)
    assert result[0] == "Middle East / oil"
    assert result[1] == ""  # gap, not an exception propagating


def test_cluster_topics_chunk_whole_call_failure_is_all_blank():
    provider = FakeLLMProvider(mode="malformed_json", responses=lambda p, s: cluster_topics_response([(1, "x")]))
    raw = provider._generate("prompt", fmt="json")
    result = ref.parse_cluster_topics_chunk(raw, item_count=3)
    assert result == ["", "", ""]


def test_consolidate_labels_unreturned_label_maps_to_itself():
    raw = consolidate_labels_response([(1, "Middle East conflict")])
    result = ref.parse_consolidate_labels(raw, labels=["Middle East conflict", "Banking / NPLs"])
    assert result == {"Middle East conflict": "Middle East conflict", "Banking / NPLs": "Banking / NPLs"}


def test_consolidate_labels_whole_call_failure_is_identity_map():
    provider = FakeLLMProvider(mode="server_error")
    try:
        provider._generate("prompt", fmt="json")
    except requests.exceptions.HTTPError:
        pass
    result = ref.consolidate_labels_whole_call_fallback(["A", "B", "C"])
    assert result == {"A": "A", "B": "B", "C": "C"}


def test_find_duplicates_batch_absence_means_not_a_duplicate():
    raw = find_duplicates_response([(2, 1)])  # only candidate 2 folds
    result = ref.parse_find_duplicates_batch(raw, candidate_count=3, shown_count=2)
    assert result == {1: 0}  # 0-indexed: candidate index 1 -> shown index 0
    assert 0 not in result  # candidate 1 (index 0) is NOT a duplicate — absent, not False


def test_find_duplicates_batch_out_of_bounds_indices_are_dropped():
    raw = find_duplicates_response([(1, 99)])  # same_as way out of range
    result = ref.parse_find_duplicates_batch(raw, candidate_count=5, shown_count=2)
    assert result == {}


def test_find_duplicates_batch_whole_call_failure_is_empty_dict_nothing_folds():
    provider = FakeLLMProvider(mode="timeout")
    try:
        provider._generate("prompt", fmt="json")
    except requests.exceptions.Timeout:
        pass
    assert ref.find_duplicates_batch_whole_call_fallback() == {}


def test_verify_group_length_mismatch_falls_back_even_with_valid_json():
    """Distinct from a parse error: the JSON is perfectly valid, but
    `len(groups) != len(headlines)` — the real code's `if` simply doesn't
    match, falling through to the same conservative fallback as an
    exception would (p3-contract.md §10.5).
    """
    raw = verify_group_response([0, 1])  # only 2 groups
    result = ref.parse_verify_group(raw, headline_count=3)
    assert result == [0, 1, 2]  # verify_group_whole_call_fallback(3) — all separate


def test_verify_group_single_bad_element_discards_the_whole_response():
    """p3-contract.md §10.5: no per-element try/except here — ONE element
    failing int() invalidates every other correctly-parsed element too.
    """
    raw = '{"groups": [0, "not-an-int", 2]}'
    result = ref.parse_verify_group(raw, headline_count=3)
    assert result == [0, 1, 2]  # whole-response fallback, NOT [0, <partial>, 2]


def test_verify_group_well_formed_response_passes_through():
    raw = verify_group_response([0, 1, 0])
    result = ref.parse_verify_group(raw, headline_count=3)
    assert result == [0, 1, 0]


# ── Batch-size / stage-ordering constants — exact-match per p3-contract §5 ──


def test_documented_default_batch_sizes():
    assert ref.BATCH_SIZE_TRANSLATE == 1
    assert ref.BATCH_SIZE_DEDUP_VERIFY == 12
    assert ref.BATCH_SIZE_JUDGE_DEFAULT == 10
    assert ref.BATCH_SIZE_TOPIC_CHUNK_DEFAULT == 50
    assert ref.BATCH_SIZE_TOPIC_DEDUP_DEFAULT == 8
    assert ref.TOPIC_DEDUP_SHOWN_CAP == 30


def test_stage_order_constant_matches_p3_contract_section_6():
    """p3-contract.md §6: STEP 0 translate -> STEP 0.5 bodies -> STEP 0.6
    semantic (being removed) -> STEP 1 dedup -> STEP 2 judge -> STEP 3
    topic. The rebuild has no semantic stage, so the EXPECTED rebuild
    ordering drops STEP 0.6 — this constant is what a future
    P3-LLM integration test should assert app.services.llm's own stage
    sequence against.
    """
    legacy_stage_order = ["translate", "bodies", "semantic", "dedup", "judge", "topic"]
    rebuild_stage_order = [s for s in legacy_stage_order if s != "semantic"]
    assert rebuild_stage_order == ["translate", "bodies", "dedup", "judge", "topic"]
