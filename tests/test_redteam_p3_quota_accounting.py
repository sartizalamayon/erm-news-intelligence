"""RED-TEAM (round 2): P3-LLM quota/token accounting -- mission's
"quota accounting under partial failure" and the coordinator's specific
"reasoning tokens" angle.

Real measurement cited by the coordinator: a `judge_batch` call on
`qwen/qwen3.6-27b` reported 3,064 prompt + 4,052 completion = 7,116
TOTAL tokens, and `gemini-flash-latest` reported 1,199
`thoughts_token_count` on a real prompt -- reasoning-token overhead
that is billed and counted against quota but invisible in a naive
prompt+completion sum.

HELD UP -- not a finding. Both real transports
(app/services/llm/transport.py) already record the PROVIDER'S OWN
reported total, never a locally computed sum:

  * `OpenAICompatibleTransport._generate()` (Groq/Mistral/any
    OpenAI-compatible host): `total_tokens = int(usage.get("total_tokens", 0))`
    read straight from the response body's `usage.total_tokens` field --
    the provider's own total, which already folds in whatever hidden
    reasoning tokens the provider itself billed.
  * `GeminiTransport._generate()`: `total_tokens = int(usage.total_token_count)`
    read from `response.usage_metadata.total_token_count` -- Gemini's
    own documented total, which INCLUDES `thoughts_token_count` for
    thinking-enabled models (the module's own docstring cites this
    exact property and the 7,116/1,199 measurements verbatim).

Both are recorded via `self._record(tokens=total_tokens)` on EVERY
attempt (success or failure, tokens=0 on failure) -- i.e. at the
TRANSPORT layer, which sits underneath both `FailoverTransport`'s
retry/candidate logic and `LLMClient`'s own except-Exception fallback
handling, so quota accounting stays accurate even when a call fails
partway through a batch or a whole-call fallback is produced further
up the stack: the token cost of the ATTEMPT is recorded regardless of
what the caller ultimately does with (or without) a successful result.

This file exists to PIN that property with a regression test, not to
report a defect -- per mission "do not inflate... if a subsystem is
genuinely solid, say so."
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest
import requests

from app.services.llm.transport import OpenAICompatibleTransport


@dataclass
class _RecordingQuotaSink:
    calls: list[tuple[str, int, int]] = field(default_factory=list)

    def record(self, provider: str, *, tokens: int, requests: int = 1) -> object:
        self.calls.append((provider, tokens, requests))
        return None


class _FakeResponseWithReasoningTokens:
    """A real-shaped OpenAI-compatible response where the provider's own
    `usage.total_tokens` (7,116, matching the coordinator's live
    measurement) is far larger than a naive prompt+visible-completion
    sum would be -- the model's <think>...</think> reasoning consumed
    real, billed tokens that never appear in the parsed `content`.
    """

    status_code = 200
    text = "{...}"

    def json(self) -> dict[str, Any]:
        return {
            "choices": [{"message": {"content": "8"}}],  # the VISIBLE answer -- one short token
            "usage": {
                "prompt_tokens": 3064,
                "completion_tokens": 4052,
                "total_tokens": 7116,  # provider's own total -- what must be recorded
            },
        }


def test_openai_compatible_transport_records_the_providers_total_not_a_local_sum(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(requests, "post", lambda *a, **k: _FakeResponseWithReasoningTokens())
    sink = _RecordingQuotaSink()
    transport = OpenAICompatibleTransport(
        base_url="https://api.example-openai-compatible.test/v1",
        api_key="test-key",
        model="qwen/qwen3.6-27b",
        provider_name="groq",
        quota=sink,
    )

    result = transport._generate("score these 10 headlines", fmt="json")

    assert result == "8"
    assert sink.calls == [("groq", 7116, 1)], (
        f"expected the provider's own total_tokens (7116) to be recorded verbatim, got {sink.calls!r} -- "
        "a locally computed prompt+completion sum would have silently under-counted this call"
    )


class _FakeGeminiUsage:
    total_token_count = 4109  # prompt + candidates + 1,199 thinking tokens, per the coordinator's live measurement


class _FakeGeminiResponse:
    text = "8"
    usage_metadata = _FakeGeminiUsage()


class _FakeGeminiModels:
    def generate_content(self, *, model: str, contents: str, config: Any) -> _FakeGeminiResponse:
        return _FakeGeminiResponse()


class _FakeGeminiClient:
    def __init__(self, *a: Any, **k: Any) -> None:
        self.models = _FakeGeminiModels()


def test_gemini_transport_records_total_token_count_including_thinking_tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    """Drives the REAL `GeminiTransport._generate()` end to end (not a
    reimplementation of its logic) -- only the `google.genai.Client`
    constructor is swapped for a fake that returns a scripted response,
    since building a real one needs a live API key / network.
    """
    import google.genai

    from app.services.llm.transport import GeminiTransport

    monkeypatch.setattr(google.genai, "Client", _FakeGeminiClient)
    sink = _RecordingQuotaSink()
    transport = GeminiTransport(api_key="test-key", model="gemini-flash-latest", quota=sink)

    result = transport._generate("score this headline", fmt="json")

    assert result == "8"
    assert sink.calls == [("gemini", 4109, 1)], (
        f"expected Gemini's total_token_count (4109, including thoughts_token_count) to be recorded "
        f"verbatim, got {sink.calls!r} -- a naive prompt+candidates sum would have missed the "
        "1,199 thinking tokens entirely"
    )
