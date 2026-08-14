"""Bounded, rate-limited parallelism for the P3 enrichment stages.

Why this exists
---------------
Every LLM stage in `app.services.enrichment` was a strictly sequential
loop. Measured on a real run (2026-08-14, 173 matched articles):

    translate   173 articles x 1 article/call  = 173 serial round-trips
    judge       173 articles / 10 per batch    =  18 serial round-trips

At a realistic ~2-4 s per call that is 6-12 minutes of pure latency
per run, essentially all of it spent waiting on a socket. Nothing about
the work is sequential — each translate call and each judge batch is
independent — so the wall-clock cost was structural, not necessary.

What this module does NOT do
----------------------------
It does not change any prompt, any batch size, or any per-call
semantics. `parallel_map` issues exactly the same calls the sequential
loop did, in the same shapes, and returns results in the SAME ORDER as
the input. This matters: P3's outputs are zipped positionally back onto
article rows (`zip(chunk, judgments)`), so an out-of-order result would
silently attach one article's score to another's. Order preservation
here is a correctness requirement, not a nicety.

Rate limiting
-------------
Free-tier providers publish per-MINUTE ceilings that raw parallelism
would blow through instantly, converting a latency win into a 429
backoff storm — the exact failure this project already hit on Groq.
`RateLimiter` is a monotonic-clock token bucket shared per provider, so
N workers collectively stay under the ceiling instead of each pacing
itself. Defaults are deliberately below the verified ceilings (see
`PROVIDER_RPM`), because the cost of being slightly conservative is a
few seconds and the cost of being wrong is a multi-minute lockout.

Failure isolation
-----------------
A raised exception in one item never fails the batch. `parallel_map`
returns `(result, exception)` per item so the caller can apply exactly
the same neutral-default fallback the sequential version applied — P3's
documented "no article is ever silently dropped" rule (p3-contract.md
§7) survives unchanged.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from typing import Any, TypeVar

from app.logging import get_logger

logger = get_logger(__name__)

T = TypeVar("T")
R = TypeVar("R")

# Requests-per-minute pacing budget per provider.
#
#   * mistral — VERIFIED live 2026-08-07 from response headers
#     (`x-ratelimit-limit-req-minute=50`). Paced at 40 to leave headroom
#     for the retry traffic in app.services.llm.transport, which also
#     consumes from the same real budget but is invisible from here.
#   * groq — the free tier publishes RPD (1,000, verified) and TPM
#     (8,000, verified) but not a documented RPM for this model. 20 is a
#     deliberately conservative floor: with an 8,000 TOKEN/minute
#     ceiling, a judge batch of 10 headlines is already token-bound long
#     before it is request-bound, so pushing RPM higher buys nothing and
#     risks the multi-minute 429 backoff this project measured.
#   * gemini / nvidia — no published or observable per-minute ceiling
#     (quota.py's own note: Gemini returns no rate-limit headers at
#     all). Paced low on purpose; an unknown ceiling is not a licence to
#     assume a high one.
# PER-KEY requests/minute. Multiplied by the number of pooled keys at
# `limiter_for()` time — the budget is per API key, so four Mistral keys
# really are 4x the ceiling, and pacing them as one key would waste 75%
# of the capacity we have.
#
# All values MEASURED live 2026-08-14 from response headers on the real
# keys, not taken from documentation:
#
#   mistral  x-ratelimit-limit-req-minute: 50, limit-tokens-minute: 50000
#   groq     x-ratelimit-limit-requests: 14400 (RPD), limit-tokens: 6000
#            (TPM); RPM is 30 per Groq's published free-tier table for
#            llama-3.1-8b-instant, which the headers do not restate.
#   gemini   Google publishes nothing and returns no rate-limit headers.
#            A 40-request burst on one key returned 14x200 then 26x429,
#            which is consistent with a ~15/min ceiling being spent
#            instantly. Paced at 10 — an unmeasurable ceiling is a reason
#            for caution, not for optimism.
#
# Each is set BELOW its measured ceiling because the transport layer
# retries on its own, and those retries spend from the same real budget
# without passing through this limiter.
PROVIDER_RPM: dict[str, int] = {
    "mistral": 40,  # measured 50
    "groq": 25,  # published 30
    "gemini": 10,  # inferred ~15
    "nvidia": 15,  # no headers, no docs — conservative
}
DEFAULT_RPM = 10

# PER-KEY tokens/minute. Requests-per-minute alone is NOT a sufficient
# model of these providers, and assuming it was cost a real debugging
# session: Groq's llama-3.1-8b-instant allows 30 requests/minute but
# only 6,000 TOKENS/minute, and ONE judge batch of 10 headlines was
# measured consuming 4,746 of them —
#
#     x-ratelimit-limit-tokens:     6000
#     x-ratelimit-remaining-tokens: 1254   (after a single call)
#     x-ratelimit-reset-tokens:     47.46s
#
# so the real ceiling is ~1.3 judge batches per minute, not 30. An
# RPM-only limiter happily let eight workers fire eight of those calls
# at once, every one of them 429'd, and the retry backoff then stacked
# across ~9 failover candidates until a single batch took minutes. The
# stage looked hung; nothing in the logs said "token budget".
#
# 0 means "no token ceiling worth modelling" (Mistral's 50,000/min is
# ~10x any batch we send; Gemini publishes nothing measurable).
PROVIDER_TPM: dict[str, int] = {
    "mistral": 50_000,  # measured: x-ratelimit-limit-tokens-minute
    "groq": 6_000,  # measured: x-ratelimit-limit-tokens
    "gemini": 0,  # not exposed by the API and not published
    "nvidia": 0,  # no headers at all
}
DEFAULT_TPM = 0

# Fallback per-call token estimate when a caller does not supply one.
# Deliberately near the measured judge-batch cost rather than a
# small/optimistic number: under-estimating spends budget the limiter
# thinks it still has, which is the exact failure this models away.
DEFAULT_CALL_TOKENS = 5_000

# Worker ceiling per stage. Kept modest because the bottleneck is the
# provider's per-minute budget, not local CPU or sockets — more workers
# than the rate limiter will admit just means more threads parked in
# `RateLimiter.acquire()`.
DEFAULT_MAX_WORKERS = 8


class RateLimiter:
    """Thread-safe token bucket, `rate_per_minute` tokens refilled
    continuously (not in per-minute steps — a stepped refill lets a full
    minute's budget fire in one burst, which is what actually trips
    provider-side limiters).

    Uses `time.monotonic()` so a system clock adjustment mid-run cannot
    hand out a windfall of tokens or stall the bucket indefinitely.
    """

    def __init__(self, rate_per_minute: int, *, burst: int | None = None, tokens_per_minute: int = 0) -> None:
        if rate_per_minute <= 0:
            raise ValueError("rate_per_minute must be positive")
        self.rate_per_minute = rate_per_minute
        self.tokens_per_minute = max(0, tokens_per_minute)
        self._rate_per_second = rate_per_minute / 60.0
        self._capacity = float(burst if burst is not None else max(1, rate_per_minute // 4))
        self._tokens = self._capacity
        # SECOND bucket, for providers whose real constraint is tokens
        # rather than requests. Starts full and refills continuously,
        # exactly like the request bucket. `tokens_per_minute == 0`
        # disables it entirely (provider has no meaningful token
        # ceiling, or does not publish one).
        self._tok_capacity = float(self.tokens_per_minute)
        self._tok_available = self._tok_capacity
        self._tok_per_second = self.tokens_per_minute / 60.0
        self._updated = time.monotonic()
        self._lock = threading.Lock()

    def _refill(self, now: float) -> None:
        """Caller must hold `self._lock`."""
        elapsed = now - self._updated
        self._tokens = min(self._capacity, self._tokens + elapsed * self._rate_per_second)
        if self._tok_capacity:
            self._tok_available = min(self._tok_capacity, self._tok_available + elapsed * self._tok_per_second)
        self._updated = now

    def acquire(self, estimated_tokens: int = 0) -> None:
        """Block until BOTH a request slot and (when the provider has a
        token ceiling) `estimated_tokens` of token budget are available,
        then consume them.

        A call larger than the whole per-minute token budget would
        otherwise wait forever; it is clamped to the full bucket, so it
        waits at most one refill period and then proceeds. Letting it
        proceed and risk one 429 is strictly better than deadlocking a
        pipeline stage.
        """
        want = min(max(0, estimated_tokens), int(self._tok_capacity)) if self._tok_capacity else 0
        while True:
            with self._lock:
                now = time.monotonic()
                self._refill(now)
                have_request = self._tokens >= 1.0
                have_tokens = (not self._tok_capacity) or self._tok_available >= want
                if have_request and have_tokens:
                    self._tokens -= 1.0
                    if self._tok_capacity:
                        self._tok_available -= want
                    return
                waits = []
                if not have_request:
                    waits.append((1.0 - self._tokens) / self._rate_per_second)
                if not have_tokens and self._tok_per_second:
                    waits.append((want - self._tok_available) / self._tok_per_second)
                wait = max(waits) if waits else 0.05
            # Sleep OUTSIDE the lock so waiting threads don't serialize
            # behind each other's sleeps.
            time.sleep(min(wait, 1.0))


_limiters: dict[str, RateLimiter] = {}
_limiters_lock = threading.Lock()


def limiter_for(provider: str, *, pool_size: int = 1) -> RateLimiter:
    """One shared limiter per provider for the process lifetime — the
    budget is per API key, so a per-call or per-stage limiter would let
    two concurrent stages each spend the full ceiling.

    `pool_size` is how many API keys this provider has available. Rate
    limits are enforced per key, so N pooled keys genuinely multiply the
    ceiling; pacing a 4-key pool at one key's rate would leave 75% of
    the paid-for capacity unused. The pool size is captured on FIRST use
    for a given provider (the limiter is process-wide and shared), and a
    later call asking for a LARGER pool widens it — never narrows it,
    so one stage that happens to resolve fewer keys cannot throttle the
    whole process.
    """
    key = (provider or "").strip().lower() or "unknown"
    keys = max(1, pool_size)
    effective_rpm = keys * PROVIDER_RPM.get(key, DEFAULT_RPM)
    effective_tpm = keys * PROVIDER_TPM.get(key, DEFAULT_TPM)
    with _limiters_lock:
        existing = _limiters.get(key)
        if existing is None or effective_rpm > existing.rate_per_minute:
            existing = RateLimiter(effective_rpm, tokens_per_minute=effective_tpm)
            _limiters[key] = existing
        return existing


def parallel_map(
    items: Sequence[T],
    fn: Callable[[T], R],
    *,
    max_workers: int = DEFAULT_MAX_WORKERS,
    limiter: RateLimiter | None = None,
    label: str = "",
    on_done: Callable[[int, int], None] | None = None,
    tokens_per_item: int = DEFAULT_CALL_TOKENS,
) -> list[tuple[R | None, Exception | None]]:
    """Apply `fn` to every item concurrently, returning
    `[(result, error), ...]` **in input order**.

    Never raises for a per-item failure — the exception is returned in
    that item's slot so the caller applies its own neutral default.
    `on_done(completed, total)` fires after each item resolves, for
    progress reporting; it is called from worker threads, so it must be
    cheap and thread-safe (the run-state updates it feeds take
    `_run_lock`, which satisfies this).
    """
    total = len(items)
    if total == 0:
        return []
    workers = max(1, min(max_workers, total))

    results: list[tuple[R | None, Exception | None]] = [(None, None)] * total
    completed = 0
    progress_lock = threading.Lock()

    def _one(index: int, item: T) -> None:
        nonlocal completed
        try:
            if limiter is not None:
                limiter.acquire(tokens_per_item)
            results[index] = (fn(item), None)
        except Exception as exc:  # noqa: BLE001 - isolation is this function's contract
            logger.warning("parallel_map_item_failed", extra={"label": label, "error": type(exc).__name__})
            results[index] = (None, exc)
        finally:
            if on_done is not None:
                with progress_lock:
                    completed += 1
                    done_now = completed
                try:
                    on_done(done_now, total)
                except Exception:  # noqa: BLE001 - progress reporting must never break the run
                    pass

    # `with` blocks until every future completes, so `results` is fully
    # populated before this function returns.
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for i, item in enumerate(items):
            pool.submit(_one, i, item)

    return results


def pool_size_of(client: Any) -> int:
    """How many API keys the client's PRIMARY provider has available.

    `FailoverTransport.candidates` is an ordered list of
    `(identity, transport)` where a pooled key's identity is
    `"<provider>:<key_id>"` (registry's `_credentials_for`). Only the
    primary provider's keys are counted: the secondary exists for
    failover, and pacing the primary as though the secondary's budget
    were also available would push the primary into 429s and force the
    failover it is meant to avoid.
    """
    transport = getattr(client, "transport", None)
    candidates = getattr(transport, "candidates", None)
    if not candidates:
        return 1
    primary = str(candidates[0][0]).split(":")[0].strip().lower()
    return sum(1 for identity, _t in candidates if str(identity).split(":")[0].strip().lower() == primary)


def provider_of(client: Any) -> str:
    """Best-effort provider name for rate-limiter selection.

    `LLMClient.name` is documented as `"<provider> · <model>"` (e.g.
    `"groq · qwen/qwen3.6-27b"`), so the provider is the part before the
    separator. Falls back to the conservative `DEFAULT_RPM` bucket
    rather than guessing a generous one — an unrecognised client must
    never end up paced FASTER than a known one.
    """
    for attr in ("provider", "provider_name"):
        value = getattr(client, attr, None)
        if isinstance(value, str) and value.strip():
            return value.strip().lower()
    name = getattr(client, "name", "")
    if isinstance(name, str) and name.strip():
        head = name.split("·")[0].strip().lower()
        if head:
            return head
    return "unknown"
