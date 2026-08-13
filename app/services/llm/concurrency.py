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
PROVIDER_RPM: dict[str, int] = {
    "mistral": 40,
    "groq": 20,
    "gemini": 12,
    "nvidia": 15,
}
DEFAULT_RPM = 12

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

    def __init__(self, rate_per_minute: int, *, burst: int | None = None) -> None:
        if rate_per_minute <= 0:
            raise ValueError("rate_per_minute must be positive")
        self._rate_per_second = rate_per_minute / 60.0
        self._capacity = float(burst if burst is not None else max(1, rate_per_minute // 4))
        self._tokens = self._capacity
        self._updated = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        """Block until one token is available, then consume it."""
        while True:
            with self._lock:
                now = time.monotonic()
                self._tokens = min(self._capacity, self._tokens + (now - self._updated) * self._rate_per_second)
                self._updated = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                deficit = 1.0 - self._tokens
                wait = deficit / self._rate_per_second
            # Sleep OUTSIDE the lock so waiting threads don't serialize
            # behind each other's sleeps.
            time.sleep(min(wait, 1.0))


_limiters: dict[str, RateLimiter] = {}
_limiters_lock = threading.Lock()


def limiter_for(provider: str) -> RateLimiter:
    """One shared limiter per provider for the process lifetime — the
    budget is per API key, so a per-call or per-stage limiter would let
    two concurrent stages each spend the full ceiling.
    """
    key = (provider or "").strip().lower() or "unknown"
    with _limiters_lock:
        existing = _limiters.get(key)
        if existing is None:
            existing = RateLimiter(PROVIDER_RPM.get(key, DEFAULT_RPM))
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
                limiter.acquire()
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
