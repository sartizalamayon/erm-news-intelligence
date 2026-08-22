"""`app.services.llm.failover.FailoverTransport` — automatic failover on
429/5xx and quota exhaustion (DoD #2), AND per-key pooling within one
provider (Task 3), proven with `FakeLLMProvider` doubles standing in
for an ordered list of `(identity, Transport)` candidates (their
`_generate()` signature is identical to the real thing — see
`app/services/llm/transport.py`'s module docstring).

The load-bearing property under test: once failover exhausts every
candidate, the exception it raises is STILL exactly the shape
`LLMClient`'s own `except Exception` (p3-contract.md §3) needs to fall
back to the documented neutral value — i.e. failover composes cleanly
with the frozen malformed-response contract rather than fighting it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from app.errors import ProviderPermanentError, ProviderRateLimitedError
from app.services.llm.failover import FailoverTransport, two_way
from app.services.llm.provider import LLMClient
from app.services.llm.quota import InMemoryQuotaStore, ProviderLimits, QuotaTracker
from app.services.llm.types import Judgment
from tests.helpers.fake_llm import FakeLLMProvider, judge_batch_response


@dataclass
class _RaisingTransport:
    """A `Transport` double that always raises a specific
    `ProviderError` — `FakeLLMProvider`'s failure modes raise
    `requests` exceptions, not `app.errors.ProviderError` subclasses
    (that mapping happens inside the real transports), so this double
    tests `FailoverTransport`'s own catch/continue logic directly and
    independent of any one real transport's error-mapping.
    """

    exc: Exception
    calls: list[str] = field(default_factory=list)

    def _generate(self, prompt: str, system: str = "", fmt: str | None = None, num_predict=None, num_ctx=None) -> str:
        self.calls.append(prompt)
        raise self.exc


def _tracker(limits: dict[str, ProviderLimits] | None = None) -> QuotaTracker:
    return QuotaTracker(store=InMemoryQuotaStore(), limits=limits or {})


def _failover(primary, secondary=None, *, quota: QuotaTracker | None = None) -> FailoverTransport:
    return two_way(quota or _tracker(), "groq", primary, "gemini" if secondary is not None else None, secondary)


def test_primary_success_never_touches_secondary() -> None:
    primary = FakeLLMProvider(responses=lambda p, s: "primary answer")
    secondary = FakeLLMProvider(responses=lambda p, s: "secondary answer")
    failover = _failover(primary, secondary)

    result = failover._generate("prompt", system="sys")
    assert result == "primary answer"
    assert len(secondary.call_log) == 0


def test_primary_429_falls_over_to_secondary() -> None:
    primary = _RaisingTransport(exc=ProviderRateLimitedError("rate limited", provider="groq"))
    secondary = FakeLLMProvider(responses=lambda p, s: "secondary answer")
    failover = _failover(primary, secondary)

    result = failover._generate("prompt")
    assert result == "secondary answer"
    assert primary.calls == ["prompt"]


def test_primary_permanent_error_also_falls_over() -> None:
    # DoD #2: "fail over when a provider is exhausted OR ERRORING" — not
    # just the retryable 429/5xx case.
    primary = _RaisingTransport(exc=ProviderPermanentError("bad key", provider="groq"))
    secondary = FakeLLMProvider(responses=lambda p, s: "secondary answer")
    failover = _failover(primary, secondary)

    assert failover._generate("prompt") == "secondary answer"


def test_both_providers_failing_raises_the_secondary_error() -> None:
    primary = _RaisingTransport(exc=ProviderRateLimitedError("groq down", provider="groq"))
    secondary = _RaisingTransport(exc=ProviderRateLimitedError("gemini down", provider="gemini"))
    failover = _failover(primary, secondary)

    with pytest.raises(ProviderRateLimitedError, match="gemini down"):
        failover._generate("prompt")


def test_no_secondary_configured_raises_primary_error_directly() -> None:
    primary = _RaisingTransport(exc=ProviderRateLimitedError("groq down", provider="groq"))
    failover = _failover(primary)

    with pytest.raises(ProviderRateLimitedError, match="groq down"):
        failover._generate("prompt")


# ── N-way pooling (Task 3: per-key pooling within one provider) ────────


def test_n_way_candidate_list_pools_across_more_than_two_entries() -> None:
    """The generalized shape `two_way()` is built on top of: THREE
    candidates (e.g. groq:key1, groq:key2, gemini:key1 — the pattern
    `app.services.llm.registry` builds when a provider has multiple
    pooled keys) are tried in order, not just a fixed primary/secondary
    pair.
    """
    key1 = _RaisingTransport(exc=ProviderRateLimitedError("key1 exhausted", provider="groq:key1"))
    key2 = _RaisingTransport(exc=ProviderRateLimitedError("key2 exhausted", provider="groq:key2"))
    key3 = FakeLLMProvider(responses=lambda p, s: "third candidate answer")

    failover = FailoverTransport(
        quota=_tracker(), candidates=[("groq:key1", key1), ("groq:key2", key2), ("gemini:key1", key3)]
    )
    result = failover._generate("prompt")

    assert result == "third candidate answer"
    assert key1.calls == ["prompt"]
    assert key2.calls == ["prompt"]


def test_n_way_candidate_quota_tracked_independently_per_key() -> None:
    """Two keys of the SAME provider must have INDEPENDENT quota — one
    key hitting its ceiling must not affect the other key's availability
    (this is the entire point of pooling: 2 keys = ~2x the effective
    ceiling, not a shared bucket that empties twice as fast).
    """
    key1 = FakeLLMProvider(responses=lambda p, s: "key1 answer")
    key2 = FakeLLMProvider(responses=lambda p, s: "key2 answer")
    tracker = _tracker(limits={"groq:key1": ProviderLimits(rpd=1), "groq:key2": ProviderLimits(rpd=1)})
    tracker.record("groq:key1", tokens=0, requests=1)  # key1 already exhausted; key2 is fresh

    failover = FailoverTransport(quota=tracker, candidates=[("groq:key1", key1), ("groq:key2", key2)])
    result = failover._generate("prompt")

    assert result == "key2 answer"
    assert len(key1.call_log) == 0  # skipped — quota exhausted before any HTTP attempt
    assert len(key2.call_log) == 1


def test_quota_exhausted_primary_skips_straight_to_secondary_without_a_call() -> None:
    primary = FakeLLMProvider(responses=lambda p, s: "should not be called")
    secondary = FakeLLMProvider(responses=lambda p, s: "secondary answer")
    tracker = _tracker(limits={"groq": ProviderLimits(rpd=1)})
    tracker.record("groq", tokens=100, requests=1)  # already at the RPD=1 ceiling

    failover = _failover(primary, secondary, quota=tracker)
    result = failover._generate("prompt")

    assert result == "secondary answer"
    assert len(primary.call_log) == 0  # never called — quota check happened BEFORE any HTTP attempt


def test_both_quota_exhausted_raises_rate_limited_error() -> None:
    primary = FakeLLMProvider()
    secondary = FakeLLMProvider()
    tracker = _tracker(limits={"groq": ProviderLimits(rpd=1), "gemini": ProviderLimits(rpd=1)})
    tracker.record("groq", tokens=0, requests=1)
    tracker.record("gemini", tokens=0, requests=1)

    failover = _failover(primary, secondary, quota=tracker)
    with pytest.raises(ProviderRateLimitedError):
        failover._generate("prompt")
    assert len(primary.call_log) == 0
    assert len(secondary.call_log) == 0


# ── composed with LLMClient — the whole-batch fallback still fires ──────


def test_failover_exhaustion_propagates_so_stage_calls_can_see_it() -> None:
    """End-to-end: both providers down -> FailoverTransport raises ->
    `judge_batch` now RAISES too (see its docstring), rather than
    catching it and returning a neutral-score fallback that read
    identically to a real judgment.

    This replaces a prior version of this test that asserted the
    opposite — `judge_batch` swallowing total failover exhaustion into a
    fake-successful-looking result. That was the exact "N/N calls ok"
    class of bug found live in the translate stage (base_url 404s on
    every call, run panel still reported success): `StageCalls`
    (`app.services.enrichment`) can only see a stage's failures if the
    exception actually reaches it. The per-article neutral fallback
    (never drop an article, p3-contract.md §7) still happens — just one
    layer up, in `enrichment.py`'s judge step, which is what actually
    calls `judge_batch` in production and is the thing with `StageCalls`
    accounting to update.
    """
    primary = _RaisingTransport(exc=ProviderRateLimitedError("groq down", provider="groq"))
    secondary = _RaisingTransport(exc=ProviderRateLimitedError("gemini down", provider="gemini"))
    failover = _failover(primary, secondary)
    client = LLMClient(transport=failover, name="failover-test")

    with pytest.raises(ProviderRateLimitedError, match="gemini down"):
        client.judge_batch([{"title_en": "A"}, {"title_en": "B"}])


def test_failover_success_on_secondary_is_transparent_to_llm_client() -> None:
    primary = _RaisingTransport(exc=ProviderRateLimitedError("groq down", provider="groq"))
    secondary = FakeLLMProvider(responses=lambda p, s: judge_batch_response([(1, 9, "top tier")]))
    failover = _failover(primary, secondary)
    client = LLMClient(transport=failover, name="failover-test")

    result = client.judge_batch([{"title_en": "A"}])
    assert result == [Judgment(relevant=True, score=9, reason="top tier")]


# ── RT-7 regression: any exception from a candidate must fall through ───
# (docs/red-team-findings.md — app/services/llm/failover.py's module
# docstring "RT-7 fix" section). Own coverage per the coordinator's
# request, independent of tests/test_redteam_p3_failover_gap.py (not
# owned by P3-LLM, never edited here).


def test_non_provider_error_from_primary_falls_over_to_secondary() -> None:
    """A completely unmodeled exception (not app.errors.ProviderError at
    all) from the primary must still try a healthy secondary — this is
    the exact gap RT-7 reported: only (ProviderRateLimitedError,
    ProviderPermanentError) used to be caught around a candidate call.
    """
    for unmodeled_exc in (
        TypeError("unexpected SDK bug"),
        IndexError("list index out of range"),
        KeyError("choices"),
        ValueError("Expecting value: line 1 column 1 (char 0)"),
    ):
        primary = _RaisingTransport(exc=unmodeled_exc)
        secondary = FakeLLMProvider(responses=lambda p, s: "healthy secondary answer")
        failover = _failover(primary, secondary)

        result = failover._generate("prompt")

        assert result == "healthy secondary answer"
        assert primary.calls == ["prompt"]
        assert len(secondary.call_log) == 1


def test_all_candidates_failing_with_non_provider_errors_raises_the_last_one() -> None:
    """When EVERY candidate fails — even with unmodeled exception types —
    something meaningful escapes: the LAST candidate's own real
    exception (type and message preserved), not a generic swallowed
    value and not a silent empty-string "success".
    """
    primary = _RaisingTransport(exc=IndexError("primary: empty choices"))
    secondary = _RaisingTransport(exc=KeyError("secondary: missing choices key"))
    failover = _failover(primary, secondary)

    with pytest.raises(KeyError, match="secondary: missing choices key"):
        failover._generate("prompt")
    assert primary.calls == ["prompt"]
    assert secondary.calls == ["prompt"]


def test_mixed_provider_error_then_unmodeled_error_still_falls_through() -> None:
    """A modeled ProviderError from the first candidate and an unmodeled
    exception from the second must BOTH fall through when a third,
    healthy candidate exists — proving the two except clauses compose
    across a chain longer than two.
    """
    key1 = _RaisingTransport(exc=ProviderRateLimitedError("groq exhausted", provider="groq"))
    key2 = _RaisingTransport(exc=TypeError("gemini SDK bug"))
    key3 = FakeLLMProvider(responses=lambda p, s: "third candidate answer")

    failover = FailoverTransport(quota=_tracker(), candidates=[("groq", key1), ("gemini", key2), ("mistral", key3)])
    result = failover._generate("prompt")

    assert result == "third candidate answer"
    assert key1.calls == ["prompt"]
    assert key2.calls == ["prompt"]


def test_quota_still_records_provider_reported_total_on_the_fallback_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """The concrete RT-7 reproduction: a REAL OpenAICompatibleTransport
    gets a well-formed HTTP 200 with an EMPTY `choices` array (a
    documented real possibility for content-filtered responses) and
    raises IndexError while parsing it — but only AFTER it already
    recorded the provider-reported `total_tokens` from that response
    into quota. Falling over to a healthy secondary must not lose that
    already-real usage record.
    """
    from app.services.llm.transport import OpenAICompatibleTransport

    class _FakeEmptyChoicesResponse:
        status_code = 200
        text = '{"choices": [], "usage": {"total_tokens": 37}}'

        def json(self) -> dict[str, object]:
            return {"choices": [], "usage": {"total_tokens": 37}}

    monkeypatch.setattr("app.services.llm.transport.requests.post", lambda *a, **kw: _FakeEmptyChoicesResponse())

    tracker = _tracker(limits={"groq": ProviderLimits(), "gemini": ProviderLimits()})
    primary = OpenAICompatibleTransport(
        base_url="https://api.example.test/v1", api_key="k", model="m", provider_name="groq", quota=tracker
    )
    secondary = FakeLLMProvider(responses=lambda p, s: "healthy secondary answer")
    failover = _failover(primary, secondary, quota=tracker)

    result = failover._generate("prompt")

    assert result == "healthy secondary answer"
    assert tracker.status("groq").used.tokens == 37  # recorded BEFORE the IndexError, not lost on fallback
