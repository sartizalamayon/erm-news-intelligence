"""Automatic failover on 429/5xx and quota exhaustion (DoD #2), AND the
mechanism per-key pooling (Task 3: "Two keys exist per provider for
Groq and Gemini — per-key quota pooling is a legitimate lever") is
built on: an ORDERED LIST of `(identity, Transport)` candidates, tried
in turn. This is the same mechanism for two different jobs — provider
failover (Groq → Gemini) and within-provider key pooling (Groq key A →
Groq key B) are structurally identical ("try this, on exhaustion/error
try the next thing"), so one class serves both. `app.services.llm.registry`
decides what `identity` strings mean (a bare provider name for
provider-level failover, or a `f"{provider}:{key_id}"`-shaped string
for per-key pooling) and in what order to build the candidate list —
this class doesn't need to know which case it's in.

`FailoverTransport` implements the SAME `Transport` interface
(`_generate(prompt, system, fmt, num_predict, num_ctx) -> str`) it
wraps, so `LLMClient` — and every ported prompt method's own `except
Exception` fallback (p3-contract.md §3) — needs ZERO changes to get
failover/pooling "for free." If every candidate is exhausted, the
exception that reaches the caller is still exactly one `except
Exception` away from the documented per-stage fallback (neutral score,
empty topic tag, etc.) — that fallback IS the correct
contract-preserving outcome once failover itself has nothing left to
try, not a bug in this layer.

Quota is checked BEFORE spending a real call: a candidate already at
its configured daily ceiling (`app.services.llm.quota.QuotaTracker`) is
skipped without an HTTP request, moving straight to the next one.

**RT-7 fix (docs/red-team-findings.md):** a candidate's `_generate()`
call is guarded by TWO except clauses, not one — the original
`(ProviderRateLimitedError, ProviderPermanentError)` clause (unchanged
behavior: logs `.retryable`, falls through) PLUS a broader
`except Exception` beneath it for anything else (an unmodeled SDK bug,
or — the concrete reproduction — `OpenAICompatibleTransport` indexing
`data["choices"][0]` into a well-formed HTTP 200 with an empty
`choices` array). Without the second clause, a healthy secondary
candidate is never tried and the run silently degrades straight to
`LLMClient`'s neutral fallback despite having somewhere real left to
go. Both clauses feed the SAME `last_exc`/fall-through loop, so "every
candidate failed" is still surfaced as one real exception (the LAST
candidate's own failure, whatever its type) once every candidate has
actually been tried — not a generic swallowed value, and not an
infinite retry: each candidate is attempted at most once per call.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.errors import ProviderPermanentError, ProviderRateLimitedError
from app.logging import get_logger
from app.services.llm.quota import QuotaTracker
from app.services.llm.transport import Transport

logger = get_logger(__name__)


@dataclass
class FailoverTransport:
    """`candidates` is an ORDERED list of `(identity, transport)` pairs
    — the first with available quota that succeeds wins. `identity` is
    whatever string `quota` tracks usage under.
    """

    quota: QuotaTracker
    candidates: list[tuple[str, Transport]] = field(default_factory=list)

    def _generate(
        self,
        prompt: str,
        system: str = "",
        fmt: str | None = None,
        num_predict: int | None = None,
        num_ctx: int | None = None,
    ) -> str:
        last_exc: Exception | None = None
        for name, transport in self.candidates:
            if self.quota.is_exhausted(name):
                logger.info("llm_candidate_skipped_quota_exhausted", extra={"candidate": name})
                last_exc = ProviderRateLimitedError(f"{name} daily quota exhausted", provider=name)
                continue
            try:
                return transport._generate(prompt, system=system, fmt=fmt, num_predict=num_predict, num_ctx=num_ctx)
            except (ProviderRateLimitedError, ProviderPermanentError) as exc:
                logger.warning(
                    "llm_candidate_failed",
                    extra={"candidate": name, "retryable": exc.retryable, "error": str(exc)},
                )
                last_exc = exc
                continue
            except Exception as exc:  # noqa: BLE001 - RT-7: ANY candidate failure must fall through to the
                # next candidate, not just a modeled ProviderError -- a malformed response body
                # (e.g. OpenAICompatibleTransport indexing an empty `choices` list) or an
                # unanticipated SDK bug must not silently strand a healthy remaining candidate.
                # LLMClient's own outer `except Exception` (p3-contract.md §3) is still the
                # ultimate backstop if EVERY candidate fails this way -- this layer's job is only
                # to keep trying while a real option remains.
                logger.warning(
                    "llm_candidate_failed_unmodeled",
                    extra={"candidate": name, "error_type": type(exc).__name__, "error": str(exc)},
                )
                last_exc = exc
                continue
        if last_exc is not None:
            raise last_exc
        # `candidates` was empty — a configuration bug one layer up
        # (app.services.llm.registry always raises ConfigurationError
        # before constructing a FailoverTransport with zero candidates;
        # this is the type-safe fallback if that invariant is ever
        # violated directly).
        raise ProviderPermanentError("no LLM provider candidates configured", provider="none")


def two_way(
    quota: QuotaTracker,
    primary_name: str,
    primary: Transport,
    secondary_name: str | None = None,
    secondary: Transport | None = None,
) -> FailoverTransport:
    """Convenience constructor for the common primary/secondary case
    (ADR §9.1's default shape) — equivalent to
    `FailoverTransport(quota, candidates=[(primary_name, primary), (secondary_name, secondary)])`
    with the secondary omitted entirely when not configured.
    """
    candidates = [(primary_name, primary)]
    if secondary is not None and secondary_name is not None:
        candidates.append((secondary_name, secondary))
    return FailoverTransport(quota=quota, candidates=candidates)
