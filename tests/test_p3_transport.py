"""`app.services.llm.transport` — `OpenAICompatibleTransport` (Groq,
Mistral, and any future OpenAI-compatible provider all go through this
ONE class — Task 3's "reusable OpenAI-compatible adapter parameterized
by base_url + key + model") and `GeminiTransport`, against MOCKED HTTP
(no real network call, no real token spent; the `_network_guard`
autouse fixture in `tests/conftest.py` would catch a real socket
attempt regardless). `OpenAICompatibleTransport` is exercised via a
fake `requests.post`; `GeminiTransport` via its SDK client, monkeypatched
the same way as before. Every transport is constructed with a
throwaway API key that is never actually sent anywhere.

Two things this suite specifically pins down, per a live-verification
finding (`docs/provider-limits-VERIFIED.md`, reproduced independently
in `docs/token-budget.md` §2 on REAL prompts): real API responses
showed `gemini-flash-latest` silently consuming ~53 invisible
"thinking" tokens on a 2-token prompt (totalTokenCount=56 vs
promptTokens=2 + candidatesTokens=1), and Groq's `qwen/qwen3.6-27b`
doing the same at much larger scale (7,116 real total tokens on a
10-headline `judge_batch` call, including a full `<think>...</think>`
block). So:

  1. Quota recording MUST use the provider's OWN reported total token
     count (`response["usage"]["total_tokens"]` for any OpenAI-compatible
     provider, `response.usage_metadata.total_token_count` for Gemini)
     — NEVER a locally computed `len(prompt) + len(completion)`-style
     sum, which cannot see hidden reasoning tokens at all.
  2. Every attempt — success or failure — records into the quota sink,
     since a rate-limited request still typically counts against a
     provider's RPD even though it consumed 0 tokens.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest
import requests

from app.errors import ProviderPermanentError, ProviderRateLimitedError
from app.services.llm.transport import GeminiTransport, OpenAICompatibleTransport


@dataclass
class _FakeQuotaSink:
    calls: list[tuple[str, int, int]] = field(default_factory=list)

    def record(self, provider: str, *, tokens: int, requests: int = 1) -> None:
        self.calls.append((provider, tokens, requests))


# ── OpenAICompatibleTransport (Groq, Mistral, ... — one class) ─────────


def _oai_transport(
    quota: _FakeQuotaSink | None = None,
    *,
    base_url: str = "https://api.groq.com/openai/v1",
    provider_name: str = "groq",
    **kwargs: Any,
) -> OpenAICompatibleTransport:
    return OpenAICompatibleTransport(
        base_url=base_url,
        api_key="test-key-never-sent",
        model="some-model",
        provider_name=provider_name,
        quota=quota,
        **kwargs,
    )


def _fake_http_response(
    status_code: int, *, total_tokens: int = 0, content: str = "ok", headers: dict[str, str] | None = None
):
    response = requests.Response()
    response.status_code = status_code
    response.headers.update(headers or {})
    if status_code < 400:
        body = {
            "choices": [{"message": {"content": content}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": total_tokens},
        }
        response._content = __import__("json").dumps(body).encode("utf-8")
    else:
        response._content = b'{"error": {"message": "boom"}}'
    return response


def test_openai_compatible_transport_records_provider_reported_total(monkeypatch: pytest.MonkeyPatch) -> None:
    """The core regression this test pins: even though prompt=10 +
    completion=5 = 15, the PROVIDER reported total_tokens=973 (simulating
    hidden reasoning/thinking tokens — this is the SAME shape Groq's
    real `qwen/qwen3.6-27b` produces, see module docstring). The
    transport must record 973, not 15.
    """
    quota = _FakeQuotaSink()
    transport = _oai_transport(quota=quota)
    fake_response = _fake_http_response(200, total_tokens=973)
    monkeypatch.setattr("app.services.llm.transport.requests.post", lambda *a, **kw: fake_response)

    result = transport._generate("prompt", system="system", fmt="json")

    assert result == "ok"
    assert quota.calls == [("groq", 973, 1)]  # NOT 15 (prompt+completion) — the provider's own total


def test_openai_compatible_transport_works_for_a_different_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """Proves the "one class, parameterized by base_url" shape: pointing
    the SAME class at Mistral's endpoint with a different provider_name
    needs no new code, just different constructor arguments.
    """
    quota = _FakeQuotaSink()
    transport = _oai_transport(quota=quota, base_url="https://api.mistral.ai/v1", provider_name="mistral")
    captured_url = {}

    def fake_post(url, **kwargs):
        captured_url["url"] = url
        return _fake_http_response(200, total_tokens=105)

    monkeypatch.setattr("app.services.llm.transport.requests.post", fake_post)
    transport._generate("prompt")

    assert captured_url["url"] == "https://api.mistral.ai/v1/chat/completions"
    assert quota.calls == [("mistral", 105, 1)]


def test_openai_compatible_transport_returns_stripped_content(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = _oai_transport()
    fake_response = _fake_http_response(200, total_tokens=100, content="  padded response  ")
    monkeypatch.setattr("app.services.llm.transport.requests.post", lambda *a, **kw: fake_response)
    assert transport._generate("prompt") == "padded response"


def test_openai_compatible_transport_ignores_num_ctx_but_accepts_it(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = _oai_transport()
    captured: dict[str, Any] = {}

    def fake_post(url, json, headers, timeout):
        captured.update(json)
        return _fake_http_response(200, total_tokens=50)

    monkeypatch.setattr("app.services.llm.transport.requests.post", fake_post)
    transport._generate("prompt", system="sys", fmt="json", num_predict=256, num_ctx=16384)

    assert "num_ctx" not in captured  # p3-contract.md §4/§8 — no client-side context knob
    assert captured["max_tokens"] == 256
    assert captured["response_format"] == {"type": "json_object"}
    assert captured["temperature"] == 0.1
    assert captured["messages"] == [{"role": "system", "content": "sys"}, {"role": "user", "content": "prompt"}]


def test_openai_compatible_transport_retries_on_429_then_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    quota = _FakeQuotaSink()
    transport = _oai_transport(quota=quota, retries=3)
    sleeps: list[float] = []
    monkeypatch.setattr("app.services.llm.transport.time.sleep", lambda s: sleeps.append(s))

    calls = {"n": 0}

    def fake_post(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return _fake_http_response(429)
        return _fake_http_response(200, total_tokens=42)

    monkeypatch.setattr("app.services.llm.transport.requests.post", fake_post)
    result = transport._generate("prompt")

    assert result == "ok"
    assert calls["n"] == 2
    assert len(sleeps) == 1
    # both the failed attempt (0 tokens) and the successful one (42) recorded
    assert quota.calls == [("groq", 0, 1), ("groq", 42, 1)]


def test_openai_compatible_transport_honors_retry_after_header(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = _oai_transport(retries=2)
    sleeps: list[float] = []
    monkeypatch.setattr("app.services.llm.transport.time.sleep", lambda s: sleeps.append(s))

    calls = {"n": 0}

    def fake_post(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return _fake_http_response(429, headers={"Retry-After": "7"})
        return _fake_http_response(200, total_tokens=10)

    monkeypatch.setattr("app.services.llm.transport.requests.post", fake_post)
    transport._generate("prompt")
    assert sleeps == [7.0]


def test_openai_compatible_transport_exhausts_retries_then_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    quota = _FakeQuotaSink()
    transport = _oai_transport(quota=quota, retries=2)
    monkeypatch.setattr("app.services.llm.transport.time.sleep", lambda s: None)
    monkeypatch.setattr("app.services.llm.transport.requests.post", lambda *a, **kw: _fake_http_response(503))

    with pytest.raises(ProviderRateLimitedError):
        transport._generate("prompt")
    assert len(quota.calls) == 2  # both attempts recorded (0 tokens each — no response received)


def test_openai_compatible_transport_permanent_error_does_not_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = _oai_transport(retries=4)
    sleeps: list[float] = []
    monkeypatch.setattr("app.services.llm.transport.time.sleep", lambda s: sleeps.append(s))
    calls = {"n": 0}

    def fake_post(*a, **kw):
        calls["n"] += 1
        return _fake_http_response(401)

    monkeypatch.setattr("app.services.llm.transport.requests.post", fake_post)
    with pytest.raises(ProviderPermanentError):
        transport._generate("prompt")
    assert calls["n"] == 1  # no retry on a non-429/5xx status
    assert sleeps == []


def test_openai_compatible_transport_timeout_retries_then_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = _oai_transport(retries=2)
    monkeypatch.setattr("app.services.llm.transport.time.sleep", lambda s: None)

    def fake_post(*a, **kw):
        raise requests.exceptions.Timeout("timed out")

    monkeypatch.setattr("app.services.llm.transport.requests.post", fake_post)
    with pytest.raises(ProviderRateLimitedError):
        transport._generate("prompt")


# ── GeminiTransport ──────────────────────────────────────────────────────


def _gemini_transport(quota: _FakeQuotaSink | None = None, **kwargs: Any) -> GeminiTransport:
    return GeminiTransport(api_key="test-key-never-sent", model="gemini-flash-lite-latest", quota=quota, **kwargs)


def _fake_gemini_response(total_tokens: int, prompt_tokens: int = 2, candidates_tokens: int = 1, text: str = "ok"):
    usage = SimpleNamespace(
        prompt_token_count=prompt_tokens, candidates_token_count=candidates_tokens, total_token_count=total_tokens
    )
    return SimpleNamespace(text=text, usage_metadata=usage)


def test_gemini_transport_records_provider_total_including_thinking_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pins the exact live-verified finding: a 2-prompt/1-candidate call
    reported totalTokenCount=56 on `gemini-flash-latest` — 53 invisible
    thinking tokens. `total_token_count` (not prompt+candidates) must be
    what gets recorded.
    """
    quota = _FakeQuotaSink()
    transport = _gemini_transport(quota=quota)
    fake_response = _fake_gemini_response(total_tokens=56, prompt_tokens=2, candidates_tokens=1)
    monkeypatch.setattr(transport._client.models, "generate_content", lambda **kw: fake_response)

    result = transport._generate("say OK", system="", fmt=None)

    assert result == "ok"
    assert quota.calls == [("gemini", 56, 1)]  # NOT 3 (prompt+candidates)


def test_gemini_transport_ignores_num_ctx(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = _gemini_transport()
    captured: dict[str, Any] = {}

    def fake_generate(**kwargs: Any):
        captured.update(kwargs)
        return _fake_gemini_response(total_tokens=10)

    monkeypatch.setattr(transport._client.models, "generate_content", fake_generate)
    transport._generate("prompt", system="sys", fmt="json", num_predict=100, num_ctx=16384)

    assert captured["config"].max_output_tokens == 100
    assert captured["config"].response_mime_type == "application/json"
    assert captured["config"].system_instruction == "sys"


def test_gemini_transport_retries_on_server_error(monkeypatch: pytest.MonkeyPatch) -> None:
    from google.genai import errors as genai_errors

    quota = _FakeQuotaSink()
    transport = _gemini_transport(quota=quota, retries=2)
    monkeypatch.setattr("app.services.llm.transport.time.sleep", lambda s: None)
    calls = {"n": 0}

    def fake_generate(**kwargs: Any):
        calls["n"] += 1
        if calls["n"] == 1:
            raise genai_errors.ServerError(503, {"error": {"message": "server error"}}, response=None)
        return _fake_gemini_response(total_tokens=20)

    monkeypatch.setattr(transport._client.models, "generate_content", fake_generate)
    result = transport._generate("prompt")

    assert result == "ok"
    assert calls["n"] == 2
    assert quota.calls == [("gemini", 0, 1), ("gemini", 20, 1)]


def test_gemini_transport_429_client_error_retries() -> None:
    from google.genai import errors as genai_errors

    transport = _gemini_transport(retries=2)
    calls = {"n": 0}

    def fake_generate(**kwargs: Any):
        calls["n"] += 1
        if calls["n"] == 1:
            raise genai_errors.ClientError(429, {"error": {"message": "rate limited"}}, response=None)
        return _fake_gemini_response(total_tokens=5)

    transport._client.models.generate_content = fake_generate
    import app.services.llm.transport as transport_module

    transport_module.time.sleep = lambda s: None
    result = transport._generate("prompt")
    assert result == "ok"
    assert calls["n"] == 2


def test_gemini_transport_non_429_client_error_is_permanent_no_retry() -> None:
    from google.genai import errors as genai_errors

    transport = _gemini_transport(retries=3)
    calls = {"n": 0}

    def fake_generate(**kwargs: Any):
        calls["n"] += 1
        raise genai_errors.ClientError(400, {"error": {"message": "bad request"}}, response=None)

    transport._client.models.generate_content = fake_generate
    with pytest.raises(ProviderPermanentError):
        transport._generate("prompt")
    assert calls["n"] == 1


def test_gemini_transport_handles_missing_total_token_count_gracefully() -> None:
    """Defensive: if a future SDK response ever omits usage metadata
    entirely, quota recording must not crash — 0 tokens, not an
    AttributeError.
    """
    quota = _FakeQuotaSink()
    transport = _gemini_transport(quota=quota)
    fake_response = SimpleNamespace(text="ok", usage_metadata=None)
    transport._client.models.generate_content = lambda **kw: fake_response

    result = transport._generate("prompt")
    assert result == "ok"
    assert quota.calls == [("gemini", 0, 1)]
