"""Validates the fake LLM provider (tests/helpers/fake_llm.py) itself —
its success shape, its five failure shapes, and that its response
factories produce JSON matching p3-contract.md §3's documented schemas.
Every later test that mocks the LLM (P3-LLM's future unit tests
included) is only as trustworthy as this file.
"""

from __future__ import annotations

import json

import pytest
import requests

from tests.helpers.fake_llm import (
    FakeLLMProvider,
    FakeProviderTimeout,
    cluster_topics_response,
    consolidate_labels_response,
    find_duplicates_response,
    judge_batch_response,
    verify_group_response,
)


def test_ok_mode_returns_default_json_body_for_json_calls():
    provider = FakeLLMProvider(mode="ok")
    assert provider._generate("prompt", system="sys", fmt="json") == "{}"


def test_ok_mode_returns_default_plain_body_for_non_json_calls():
    provider = FakeLLMProvider(mode="ok")
    assert provider._generate("prompt", system="sys") == "ok"


def test_custom_responses_callable_is_used_when_provided():
    provider = FakeLLMProvider(mode="ok", responses=lambda prompt, system: f"echo:{prompt}")
    assert provider._generate("hello", system="sys") == "echo:hello"


def test_call_log_records_every_call_with_full_signature():
    provider = FakeLLMProvider(mode="ok")
    provider._generate("p1", system="s1", fmt="json", num_predict=2048, num_ctx=8192)
    provider._generate("p2")
    assert provider.call_log == [
        {"prompt": "p1", "system": "s1", "fmt": "json", "num_predict": 2048, "num_ctx": 8192},
        {"prompt": "p2", "system": "", "fmt": None, "num_predict": None, "num_ctx": None},
    ]


def test_malformed_json_mode_returns_text_that_fails_json_loads():
    provider = FakeLLMProvider(mode="malformed_json", responses=lambda p, s: judge_batch_response([(1, 8, "ok")]))
    raw = provider._generate("prompt", fmt="json")
    with pytest.raises(json.JSONDecodeError):
        json.loads(raw)


def test_truncated_mode_returns_a_valid_json_prefix_that_still_fails_to_parse():
    """Distinct failure signature from malformed_json (p3-contract.md §3's
    parsing sits inside `except Exception`, so both must be swallowed —
    but they exercise different code paths: outright garbage vs. a
    plausible-looking cut-off object).
    """
    full = judge_batch_response([(1, 8, "telecom relevance"), (2, 3, "sports, excluded")])
    provider = FakeLLMProvider(mode="truncated", responses=lambda p, s: full)
    raw = provider._generate("prompt", fmt="json")
    assert raw != full
    assert full.startswith(raw)  # truncation is a strict prefix, not garbage
    with pytest.raises(json.JSONDecodeError):
        json.loads(raw)


def test_rate_limited_mode_raises_http_error_with_429():
    provider = FakeLLMProvider(mode="rate_limited")
    with pytest.raises(requests.exceptions.HTTPError) as exc_info:
        provider._generate("prompt", fmt="json")
    assert exc_info.value.response.status_code == 429


def test_server_error_mode_raises_http_error_with_5xx():
    provider = FakeLLMProvider(mode="server_error")
    with pytest.raises(requests.exceptions.HTTPError) as exc_info:
        provider._generate("prompt", fmt="json")
    assert 500 <= exc_info.value.response.status_code < 600


def test_timeout_mode_raises_requests_timeout_subclass():
    provider = FakeLLMProvider(mode="timeout")
    with pytest.raises(requests.exceptions.Timeout):
        provider._generate("prompt", fmt="json")
    with pytest.raises(FakeProviderTimeout):
        provider._generate("prompt", fmt="json")


def test_failure_modes_still_record_the_call_before_raising():
    """A caller inspecting call_log after a caught exception should still
    see the attempt — mirrors real provider behavior where the request
    was genuinely sent before it failed.
    """
    provider = FakeLLMProvider(mode="rate_limited")
    with pytest.raises(requests.exceptions.HTTPError):
        provider._generate("did this happen", fmt="json")
    assert provider.call_log[0]["prompt"] == "did this happen"


# ── Response-body factories match p3-contract.md §3's schemas ──────────


def test_judge_batch_response_schema():
    raw = judge_batch_response([(1, 8, "telecom"), (2, 3, "sports")])
    data = json.loads(raw)
    assert data == {"scores": [{"n": 1, "score": 8, "reason": "telecom"}, {"n": 2, "score": 3, "reason": "sports"}]}


def test_cluster_topics_response_schema():
    raw = cluster_topics_response([(1, "Middle East / oil"), (2, "")])
    data = json.loads(raw)
    assert data == {"labels": [{"n": 1, "label": "Middle East / oil"}, {"n": 2, "label": ""}]}


def test_consolidate_labels_response_schema():
    raw = consolidate_labels_response([(1, "Middle East conflict"), (2, "Middle East conflict")])
    data = json.loads(raw)
    assert data == {
        "map": [{"n": 1, "canon": "Middle East conflict"}, {"n": 2, "canon": "Middle East conflict"}]
    }


def test_find_duplicates_response_schema():
    raw = find_duplicates_response([(3, 1)])
    data = json.loads(raw)
    assert data == {"dupes": [{"c": 3, "same_as": 1}]}


def test_find_duplicates_response_empty_means_nothing_folds():
    raw = find_duplicates_response([])
    assert json.loads(raw) == {"dupes": []}


def test_verify_group_response_schema():
    raw = verify_group_response([0, 1, 0])
    assert json.loads(raw) == {"groups": [0, 1, 0]}
