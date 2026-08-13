"""Tests for `app.services.llm.concurrency` — the bounded, rate-limited
parallel executor the P3 stages use.

The invariants here are not performance ones. Order preservation and
failure isolation are CORRECTNESS requirements: P3 zips results back
onto article rows positionally (`zip(chunk, judgments)`), so a
reordered result set would attach one article's relevance score to a
different article, silently and unrecoverably.

Nothing in this file spends a real LLM token — every `fn` is local.
"""

from __future__ import annotations

import threading
import time

import pytest

from app.services.llm.concurrency import (
    DEFAULT_RPM,
    PROVIDER_RPM,
    RateLimiter,
    limiter_for,
    parallel_map,
    provider_of,
)

# ── parallel_map: ordering and isolation ─────────────────────────────────


def test_results_are_returned_in_input_order_despite_uneven_durations():
    """The first item is made the SLOWEST so a naive
    completion-order implementation would visibly reorder.
    """
    items = list(range(12))

    def fn(i: int) -> int:
        time.sleep(0.05 if i == 0 else 0.001)
        return i * 10

    out = parallel_map(items, fn, max_workers=6)
    assert [value for value, _err in out] == [i * 10 for i in items]
    assert all(err is None for _v, err in out)


def test_one_failing_item_does_not_fail_the_batch():
    def fn(i: int) -> int:
        if i == 3:
            raise ValueError("boom")
        return i

    out = parallel_map(list(range(6)), fn, max_workers=4)
    assert out[3][0] is None
    assert isinstance(out[3][1], ValueError)
    # Every other slot still holds its real result, in place.
    assert [v for i, (v, _e) in enumerate(out) if i != 3] == [0, 1, 2, 4, 5]


def test_every_item_is_applied_exactly_once():
    seen: list[int] = []
    lock = threading.Lock()

    def fn(i: int) -> int:
        with lock:
            seen.append(i)
        return i

    parallel_map(list(range(50)), fn, max_workers=8)
    assert sorted(seen) == list(range(50))


def test_empty_input_returns_empty_and_calls_nothing():
    called = []
    assert parallel_map([], lambda x: called.append(x), max_workers=4) == []
    assert called == []


def test_progress_callback_reports_each_completion_and_reaches_total():
    reports: list[tuple[int, int]] = []
    lock = threading.Lock()

    def on_done(done: int, total: int) -> None:
        with lock:
            reports.append((done, total))

    parallel_map(list(range(10)), lambda x: x, max_workers=4, on_done=on_done)
    assert len(reports) == 10
    assert max(done for done, _t in reports) == 10
    assert all(total == 10 for _d, total in reports)


def test_a_raising_progress_callback_never_breaks_the_run():
    """Progress reporting is decoration; it must not be able to fail a
    pipeline stage.
    """

    def bad_on_done(_done: int, _total: int) -> None:
        raise RuntimeError("progress exploded")

    out = parallel_map([1, 2, 3], lambda x: x * 2, max_workers=2, on_done=bad_on_done)
    assert [v for v, _e in out] == [2, 4, 6]


def test_work_actually_runs_concurrently():
    """Guards against a refactor silently collapsing this back to a
    sequential loop — the whole reason the module exists.
    """
    items = list(range(8))
    started = time.monotonic()
    parallel_map(items, lambda _i: time.sleep(0.05), max_workers=8)
    elapsed = time.monotonic() - started
    assert elapsed < 0.05 * len(items) / 2


# ── RateLimiter ──────────────────────────────────────────────────────────


def test_rate_limiter_admits_burst_then_throttles():
    # 60/min = 1/sec, burst capacity 60//4 = 15.
    limiter = RateLimiter(60)
    started = time.monotonic()
    for _ in range(15):
        limiter.acquire()
    burst_elapsed = time.monotonic() - started
    assert burst_elapsed < 0.2, "the initial burst should not be throttled"

    # The 16th must wait for a refill (~1s at 1 token/sec).
    limiter.acquire()
    assert time.monotonic() - started > 0.5


def test_rate_limiter_is_safe_under_concurrent_acquire():
    limiter = RateLimiter(600, burst=5)
    counter = {"n": 0}
    lock = threading.Lock()

    def worker() -> None:
        limiter.acquire()
        with lock:
            counter["n"] += 1

    threads = [threading.Thread(target=worker) for _ in range(40)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)
    assert counter["n"] == 40


def test_rate_limiter_rejects_a_nonsense_rate():
    with pytest.raises(ValueError):
        RateLimiter(0)


def test_limiter_is_shared_per_provider():
    """The budget belongs to the API key, so two call sites asking for
    the same provider must get the SAME bucket — otherwise each stage
    would independently spend the full ceiling.
    """
    assert limiter_for("mistral") is limiter_for("mistral")
    assert limiter_for("mistral") is limiter_for("  MISTRAL  ")
    assert limiter_for("mistral") is not limiter_for("groq")


# ── provider_of ──────────────────────────────────────────────────────────


def test_provider_is_parsed_from_the_client_name():
    class Client:
        name = "groq · qwen/qwen3.6-27b"

    assert provider_of(Client()) == "groq"


def test_explicit_provider_attribute_wins():
    class Client:
        provider = "mistral"
        name = "groq · something"

    assert provider_of(Client()) == "mistral"


def test_unknown_client_falls_back_to_the_conservative_bucket():
    class Client:
        pass

    assert provider_of(Client()) == "unknown"
    # An unrecognised provider must never be paced FASTER than a known one.
    assert PROVIDER_RPM.get("unknown", DEFAULT_RPM) <= min(PROVIDER_RPM.values())
