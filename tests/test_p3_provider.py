"""`app.services.llm.provider.LLMClient` — the common interface over
Groq and Google AI Studio (DoD #1), exercised with
`tests/helpers/fake_llm.py`'s `FakeLLMProvider` standing in for
`Transport` (their `_generate()` signatures are identical by
construction — see `app/services/llm/transport.py`'s module docstring).

Every method here is checked for TWO things: (1) it builds the exact
prompt/system text `app.services.llm.prompts` would (spot-checked via
`FakeLLMProvider.call_log`, the full byte-exact comparison lives in
`tests/test_p3_prompts.py`), and (2) it applies the documented
malformed-response fallback when the transport raises — i.e. that
`LLMClient` wires `prompts` + `parsing` + `Transport` together
correctly, not just that each piece works in isolation.

No LLM tokens are spent.
"""

from __future__ import annotations

from app.services.llm.provider import LLMClient
from app.services.llm.types import Judgment, Summary
from tests.helpers.fake_llm import (
    FakeLLMProvider,
    cluster_topics_response,
    consolidate_labels_response,
    find_duplicates_response,
    judge_batch_response,
    verify_group_response,
)


def _client(**fake_kwargs) -> tuple[LLMClient, FakeLLMProvider]:
    fake = FakeLLMProvider(**fake_kwargs)
    return LLMClient(transport=fake, name="fake · test-model"), fake


# ── judge_batch ──────────────────────────────────────────────────────────


def test_judge_batch_success_returns_parsed_scores() -> None:
    client, fake = _client(responses=lambda p, s: judge_batch_response([(1, 9, "top"), (2, 2, "noise")]))
    items = [{"title_en": "Headline A"}, {"title_en": "Headline B"}]
    result = client.judge_batch(items)
    assert result == [
        Judgment(relevant=True, score=9, reason="top"),
        Judgment(relevant=False, score=2, reason="noise"),
    ]
    assert len(fake.call_log) == 1
    assert fake.call_log[0]["fmt"] == "json"
    assert "Headline A" in fake.call_log[0]["prompt"]
    assert "Headline B" in fake.call_log[0]["prompt"]


def test_judge_batch_empty_items_returns_empty_without_a_call() -> None:
    client, fake = _client()
    assert client.judge_batch([]) == []
    assert fake.call_log == []


def test_judge_batch_transport_failure_falls_back_to_neutral_for_every_item() -> None:
    client, fake = _client(mode="rate_limited")
    items = [{"title_en": "A"}, {"title_en": "B"}, {"title_en": "C"}]
    result = client.judge_batch(items)
    assert len(result) == 3
    assert all(j.score == 5 and j.relevant is True for j in result)
    assert all(j.reason == result[0].reason for j in result)  # same error string on every item
    assert len(fake.call_log) == 1  # the call WAS attempted before failing


def test_judge_batch_malformed_json_falls_back_to_neutral() -> None:
    client, _ = _client(mode="malformed_json", responses=lambda p, s: judge_batch_response([(1, 8, "x")]))
    result = client.judge_batch([{"title_en": "A"}])
    assert result == [Judgment(relevant=True, score=5, reason=result[0].reason)]
    assert result[0].reason.startswith("llm error:")


def test_judge_batch_system_includes_fixed_guide() -> None:
    client, fake = _client(responses=lambda p, s: judge_batch_response([(1, 5, "")]))
    client.judge_batch([{"title_en": "A"}])
    assert "Grameenphone" in fake.call_log[0]["system"]
    assert "Respond ONLY with JSON" in fake.call_log[0]["system"]


# ── judge (single, dead but implemented) ────────────────────────────────


def test_judge_single_success() -> None:
    client, _ = _client(responses=lambda p, s: '{"score": 8, "reason": "telecom news"}')
    result = client.judge("GP launches 5G", "Daily Star", "telecom")
    assert result == Judgment(relevant=True, score=8, reason="telecom news")


def test_judge_single_failure_fallback() -> None:
    client, _ = _client(mode="timeout")
    result = client.judge("Some headline", "Source", "")
    assert result.score == 5
    assert result.relevant is True
    assert result.reason.startswith("llm error:")


# ── cluster_topics_chunk ─────────────────────────────────────────────────


def test_cluster_topics_chunk_success() -> None:
    labels = [(1, "Middle East / oil"), (2, "Banking / NPLs")]
    client, fake = _client(responses=lambda p, s: cluster_topics_response(labels))
    items = [{"title_en": "Iran news"}, {"title_en": "Bank news"}]
    result = client.cluster_topics_chunk(items, known_labels=[])
    assert result == ["Middle East / oil", "Banking / NPLs"]
    assert fake.call_log[0]["num_predict"] == 4096


def test_cluster_topics_chunk_known_labels_appear_in_system() -> None:
    client, fake = _client(responses=lambda p, s: cluster_topics_response([(1, "Middle East / oil")]))
    client.cluster_topics_chunk([{"title_en": "A"}], known_labels=["Middle East / oil"])
    assert "TOPICS ALREADY IN USE" in fake.call_log[0]["system"]
    assert "Middle East / oil" in fake.call_log[0]["system"]


def test_cluster_topics_chunk_empty_items_short_circuits() -> None:
    client, fake = _client()
    assert client.cluster_topics_chunk([], known_labels=[]) == []
    assert fake.call_log == []


def test_cluster_topics_chunk_failure_all_untagged() -> None:
    client, _ = _client(mode="server_error")
    result = client.cluster_topics_chunk([{"title_en": "A"}, {"title_en": "B"}], known_labels=[])
    assert result == ["", ""]


# ── consolidate_labels ───────────────────────────────────────────────────


def test_consolidate_labels_success() -> None:
    client, fake = _client(responses=lambda p, s: consolidate_labels_response([(1, "Middle East"), (2, "Middle East")]))
    result = client.consolidate_labels(["Middle East conflict", "Iran-US tensions"])
    assert result == {"Middle East conflict": "Middle East", "Iran-US tensions": "Middle East"}
    assert fake.call_log[0]["num_predict"] == 2048


def test_consolidate_labels_fewer_than_two_labels_short_circuits_no_call() -> None:
    client, fake = _client()
    assert client.consolidate_labels(["Only one"]) == {"Only one": "Only one"}
    assert client.consolidate_labels([]) == {}
    assert fake.call_log == []


def test_consolidate_labels_filters_blank_labels_first() -> None:
    client, fake = _client(responses=lambda p, s: consolidate_labels_response([(1, "A"), (2, "B")]))
    result = client.consolidate_labels(["A", "", "B"])
    assert "" not in result
    assert fake.call_log  # a real call happened since 2 non-blank labels remained


def test_consolidate_labels_failure_is_identity_map() -> None:
    client, _ = _client(mode="malformed_json", responses=lambda p, s: consolidate_labels_response([(1, "A")]))
    result = client.consolidate_labels(["A", "B"])
    assert result == {"A": "A", "B": "B"}


# ── find_duplicates_batch ────────────────────────────────────────────────


def test_find_duplicates_batch_success() -> None:
    client, fake = _client(responses=lambda p, s: find_duplicates_response([(1, 1)]))
    shown = [{"title_en": "Shown story", "source": "BBC"}]
    candidates = [{"title_en": "Restatement", "source": "CNN"}]
    result = client.find_duplicates_batch(shown, candidates)
    assert result == {0: 0}
    assert fake.call_log[0]["num_predict"] == 1024
    assert fake.call_log[0]["num_ctx"] is not None


def test_find_duplicates_batch_empty_candidates_short_circuits() -> None:
    client, fake = _client()
    assert client.find_duplicates_batch([{"title_en": "shown"}], []) == {}
    assert fake.call_log == []


def test_find_duplicates_batch_failure_folds_nothing() -> None:
    client, _ = _client(mode="rate_limited")
    result = client.find_duplicates_batch([], [{"title_en": "A"}])
    assert result == {}


def test_find_duplicates_batch_shown_capped_at_30() -> None:
    client, fake = _client(responses=lambda p, s: find_duplicates_response([]))
    shown = [{"title_en": f"Story {i}", "source": "S"} for i in range(40)]
    client.find_duplicates_batch(shown, [{"title_en": "Candidate", "source": "C"}])
    # only S1..S30 should appear as reference labels in the prompt
    assert "S31." not in fake.call_log[0]["prompt"]
    assert "S30." in fake.call_log[0]["prompt"]


# ── verify_group ─────────────────────────────────────────────────────────


def test_verify_group_single_or_empty_headline_short_circuits_no_call() -> None:
    client, fake = _client()
    assert client.verify_group([]) == []
    assert client.verify_group(["only one"]) == [0]
    assert fake.call_log == []


def test_verify_group_success() -> None:
    client, _ = _client(responses=lambda p, s: verify_group_response([0, 1, 0]))
    result = client.verify_group(["A", "B", "C"])
    assert result == [0, 1, 0]


def test_verify_group_failure_is_all_separate() -> None:
    client, _ = _client(mode="timeout")
    result = client.verify_group(["A", "B", "C"])
    assert result == [0, 1, 2]


# ── translate_title ───────────────────────────────────────────────────────


def test_translate_title_success() -> None:
    client, fake = _client(responses=lambda p, s: '"Translated Headline"')
    result = client.translate_title("বাংলা শিরোনাম")
    assert result == "Translated Headline"
    # p3-contract.md §1.7: the raw title IS the prompt, no template
    assert fake.call_log[0]["prompt"] == "বাংলা শিরোনাম"
    assert fake.call_log[0]["fmt"] is None


def test_translate_title_failure_returns_empty_string() -> None:
    client, _ = _client(mode="server_error")
    assert client.translate_title("Some title") == ""


# ── summarize (dead) ────────────────────────────────────────────────────


def test_summarize_success() -> None:
    client, _ = _client(responses=lambda p, s: "A short summary.")
    result = client.summarize("Title", "Source", "Body")
    assert result == Summary(text="A short summary.")


def test_summarize_failure_returns_empty_summary() -> None:
    client, _ = _client(mode="timeout")
    assert client.summarize("Title", "Source") == Summary(text="")


# ── draft_bullet ─────────────────────────────────────────────────────────


def test_draft_bullet_success_strips_leading_bullet() -> None:
    client, _ = _client(responses=lambda p, s: "• Port tension – something happened")
    result = client.draft_bullet("Title", "Body", "Macro")
    assert result == "Port tension – something happened"


def test_draft_bullet_failure_returns_original_title() -> None:
    client, _ = _client(mode="rate_limited")
    result = client.draft_bullet("Original Headline", "Body", "Macro")
    assert result == "Original Headline"


# ── suggest_guidance — NO try/except, propagates on failure ─────────────


def test_suggest_guidance_success() -> None:
    client, _ = _client(responses=lambda p, s: "Add more weight to port disruption stories.")
    result = client.suggest_guidance(["Approved 1"], ["Rejected 1"])
    assert result == "Add more weight to port disruption stories."


def test_suggest_guidance_failure_propagates_the_exception() -> None:
    import pytest
    import requests

    client, _ = _client(mode="rate_limited")
    with pytest.raises(requests.exceptions.HTTPError):
        client.suggest_guidance(["A"], ["R"])
