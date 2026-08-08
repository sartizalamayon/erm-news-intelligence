"""RED-TEAM (round 2): `app.services.llm.failover.FailoverTransport`
attack surface — mission's "provider failover correctness".

Finding (see docs/red-team-findings.md, RT-7): `FailoverTransport._generate()`
only catches `(ProviderRateLimitedError, ProviderPermanentError)` around
each candidate's `transport._generate()` call:

    try:
        return transport._generate(...)
    except (ProviderRateLimitedError, ProviderPermanentError) as exc:
        ...
        continue

Any OTHER exception type propagates straight out of `FailoverTransport`
without trying the next candidate. `app.services.llm.provider.LLMClient`
still has its own outer `except Exception` (p3-contract.md §3), so this
does not crash the whole enrichment run — but it silently skips every
remaining candidate (secondary provider, backup keys under the new
per-key-pooling design in `failover.py`'s own module docstring) and
goes straight to the documented whole-call FALLBACK VALUE, even when a
perfectly healthy candidate was next in line.

This is realistic, not just synthetic: `OpenAICompatibleTransport._generate()`
(app/services/llm/transport.py) parses the response body AFTER its own
try/except block (which only wraps the `requests.post()` call itself):

    data = response.json()                          # can raise json.JSONDecodeError
    ...
    content = data["choices"][0]["message"]["content"]  # can raise IndexError/KeyError

An HTTP 200 with a malformed/truncated JSON body, or a 200 with an
empty `choices` array (a real, documented possibility with OpenAI-
compatible APIs — content-filtered or otherwise degenerate responses),
raises `json.JSONDecodeError`/`IndexError` from INSIDE the `else`-style
parsing code, which is not one of the two caught types anywhere in the
call chain until `LLMClient`'s outer catch. Reproduced below both
generically (any non-ProviderError exception) and concretely (a mocked
200 response with an empty `choices` list, through the REAL
`OpenAICompatibleTransport`, not a synthetic double).

Severity: medium-high. Does not crash the process (LLMClient's outer
`except Exception` still produces the documented fallback), but
silently defeats the specific mechanism DoD #2 requires ("automatic
failover on 429/5xx and quota exhaustion") for a realistic response
shape, degrading a batch to neutral/empty fallback values instead of a
real score/label from a healthy secondary candidate that was never even
tried.

FIXED by P3-LLM (commit 463c7fa): `FailoverTransport._generate()` now
has a second, broader `except Exception` beneath the modeled
`(ProviderRateLimitedError, ProviderPermanentError)` clause, so ANY
candidate failure falls through to the next candidate. Re-verified
below (flipped from xfail to normal regression tests) plus two
additional attacks: all-candidates-fail still raises the LAST
candidate's real exception (not swallowed, not generic, not an
infinite retry -- one attempt per candidate), and quota/token
accounting still records `tokens=0` for each failed attempt via the
transport's own `_record()` call before it raises, independent of
whether FailoverTransport's outer catch is the modeled or broad clause.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest
import requests

from app.services.llm.failover import two_way
from app.services.llm.quota import InMemoryQuotaStore, ProviderLimits, QuotaTracker
from app.services.llm.transport import OpenAICompatibleTransport


def _tracker() -> QuotaTracker:
    return QuotaTracker(store=InMemoryQuotaStore(), limits={"groq": ProviderLimits(), "gemini": ProviderLimits()})


@dataclass
class _BuggyTransport:
    """Raises something that is NOT `app.errors.ProviderError` -- a
    stand-in for any unmodeled bug or unexpected exception shape from a
    real transport's response-parsing code (see module docstring for
    the concrete IndexError/JSONDecodeError trigger this represents).
    """

    exc: Exception
    calls: list[str] = field(default_factory=list)

    def _generate(self, prompt: str, system: str = "", fmt: str | None = None, num_predict=None, num_ctx=None) -> str:
        self.calls.append(prompt)
        raise self.exc


@dataclass
class _HealthyTransport:
    calls: list[str] = field(default_factory=list)

    def _generate(self, prompt: str, system: str = "", fmt: str | None = None, num_predict=None, num_ctx=None) -> str:
        self.calls.append(prompt)
        return "a real answer from the healthy candidate"


@pytest.mark.parametrize(
    "unmodeled_exc",
    [
        TypeError("unexpected SDK bug, not a modeled ProviderError"),
        IndexError("list index out of range"),  # data["choices"][0] on an empty list
        KeyError("choices"),  # a response body missing the key entirely
        ValueError("Expecting value: line 1 column 1 (char 0)"),  # json.JSONDecodeError is a ValueError subclass
    ],
    ids=["typeerror", "indexerror", "keyerror", "jsondecodeerror-shaped"],
)
def test_unmodeled_exception_from_primary_skips_a_healthy_secondary(unmodeled_exc: Exception) -> None:
    """RT-7, CONFIRMED FIXED (P3-LLM, 463c7fa) -- was xfail(strict=True)."""
    primary = _BuggyTransport(exc=unmodeled_exc)
    secondary = _HealthyTransport()
    failover = two_way(_tracker(), "groq", primary, "gemini", secondary)

    result = failover._generate("score this headline")

    assert result == "a real answer from the healthy candidate", (
        f"a healthy secondary candidate was configured but never even called (secondary.calls="
        f"{secondary.calls!r}) -- an unmodeled {type(unmodeled_exc).__name__} from the primary "
        "aborted the whole failover chain instead of trying the next candidate"
    )
    assert secondary.calls, "the secondary candidate should have been tried at least once"


class _FakeJsonResponse:
    """Stands in for `requests.Response` at the exact seam
    `OpenAICompatibleTransport._generate()` uses (`.status_code`, `.json()`,
    `.text`) -- a real HTTP 200 whose body is well-formed JSON but has an
    empty `choices` array, a documented possibility for content-filtered
    or otherwise degenerate OpenAI-compatible responses.
    """

    status_code = 200
    text = '{"choices": [], "usage": {"total_tokens": 12}}'

    def json(self) -> dict[str, Any]:
        return {"choices": [], "usage": {"total_tokens": 12}}


def test_empty_choices_array_from_real_transport_skips_a_healthy_secondary(monkeypatch: pytest.MonkeyPatch) -> None:
    """RT-7, CONFIRMED FIXED (P3-LLM, 463c7fa) -- was xfail(strict=True)."""
    monkeypatch.setattr(requests, "post", lambda *a, **k: _FakeJsonResponse())

    primary = OpenAICompatibleTransport(
        base_url="https://api.example-openai-compatible.test/v1",
        api_key="test-key",
        model="test-model",
        provider_name="groq",
    )
    secondary = _HealthyTransport()
    failover = two_way(_tracker(), "groq", primary, "gemini", secondary)

    result = failover._generate("score this headline")

    assert result == "a real answer from the healthy candidate", (
        f"the healthy secondary was never tried (secondary.calls={secondary.calls!r}) -- an empty "
        "'choices' array from the primary's real HTTP 200 response aborted the whole failover chain"
    )


def test_all_candidates_failing_with_unmodeled_exceptions_raises_meaningfully_not_infinitely() -> None:
    """Attacks the fix itself: if EVERY candidate fails with an unmodeled
    exception, FailoverTransport must still raise something real (the
    last candidate's own exception, not a swallowed/generic value) and
    must try each candidate exactly once -- no infinite retry loop.
    """
    primary = _BuggyTransport(exc=TypeError("primary bug"))
    secondary = _BuggyTransport(exc=IndexError("secondary bug"))
    failover = two_way(_tracker(), "groq", primary, "gemini", secondary)

    with pytest.raises(IndexError, match="secondary bug"):
        failover._generate("score this headline")

    assert len(primary.calls) == 1, "primary should be attempted exactly once, not retried"
    assert len(secondary.calls) == 1, "secondary should be attempted exactly once, not retried"


def test_token_totals_still_recorded_when_a_candidate_fails_with_an_unmodeled_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Quota/token accounting must stay accurate on the fallback path
    too: a real OpenAICompatibleTransport candidate that fails with an
    unmodeled exception (empty choices array) still records tokens=0 for
    that attempt via its own _record() call before raising -- independent
    of which FailoverTransport except clause ends up catching it.
    """
    sink_calls: list[tuple[str, int, int]] = []

    class _RecordingSink:
        def record(self, provider: str, *, tokens: int, requests: int = 1) -> None:
            sink_calls.append((provider, tokens, requests))

    monkeypatch.setattr(requests, "post", lambda *a, **k: _FakeJsonResponse())
    # Swap the transport's own quota sink for one we can inspect directly
    # -- the transport records tokens=0 on this failure ITSELF, before
    # FailoverTransport's except clauses ever see the exception.
    primary = OpenAICompatibleTransport(
        base_url="https://api.example-openai-compatible.test/v1",
        api_key="test-key",
        model="test-model",
        provider_name="groq",
        quota=_RecordingSink(),
    )
    secondary = _HealthyTransport()
    failover = two_way(_tracker(), "groq", primary, "gemini", secondary)

    result = failover._generate("score this headline")

    assert result == "a real answer from the healthy candidate"
    # The real transport reads `usage.total_tokens` BEFORE indexing into
    # `choices` (see OpenAICompatibleTransport._generate()), so a
    # response with a real usage block but an empty choices array still
    # records the provider's true token cost (12, from _FakeJsonResponse)
    # even though the call ultimately raises -- better than tokens=0,
    # and worth pinning: this is real cost the provider billed.
    assert sink_calls == [("groq", 12, 1)], (
        f"expected the failed primary attempt to still record the provider's real usage.total_tokens "
        f"(12) even though parsing the answer itself failed, got {sink_calls!r}"
    )
