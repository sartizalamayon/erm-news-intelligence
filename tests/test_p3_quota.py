"""`app.services.llm.quota` — per-provider-per-day token/request
tracking against admin-configured ceilings (DoD #2). ALL limits are
runtime config (`ProviderLimits`), never a hardcoded constant consulted
directly by the tracking logic — `DEFAULT_LIMITS` is only a documented
starting point `load_provider_limits()` returns when nothing has been
configured yet.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.db.testing import migrated_schema, pg_available
from app.services.llm.quota import (
    DEFAULT_LIMITS,
    InMemoryQuotaStore,
    ProviderLimits,
    QuotaTracker,
    QuotaUsage,
    SettingsQuotaStore,
    load_provider_limits,
    save_provider_limits,
)

# ── QuotaStatus / ProviderLimits pure logic (no DB needed) ──────────────


def test_quota_status_remaining_and_exhausted_with_known_ceiling() -> None:
    tracker = QuotaTracker(store=InMemoryQuotaStore(), limits={"groq": ProviderLimits(tpd=1000, rpd=10)})
    today = date(2026, 8, 6)

    status = tracker.status("groq", today)
    assert status.used == QuotaUsage()
    assert status.tokens_remaining == 1000
    assert status.requests_remaining == 10
    assert status.exhausted is False

    tracker.record("groq", tokens=999, requests=9, day=today)
    status = tracker.status("groq", today)
    assert status.tokens_remaining == 1
    assert status.requests_remaining == 1
    assert status.exhausted is False

    tracker.record("groq", tokens=1, requests=0, day=today)
    assert tracker.status("groq", today).exhausted is True  # tokens hit ceiling exactly


def test_quota_exhausted_on_request_ceiling_alone() -> None:
    tracker = QuotaTracker(store=InMemoryQuotaStore(), limits={"groq": ProviderLimits(tpd=None, rpd=2)})
    today = date(2026, 8, 6)
    tracker.record("groq", tokens=100_000, requests=1, day=today)
    assert tracker.is_exhausted("groq", today) is False
    tracker.record("groq", tokens=0, requests=1, day=today)
    assert tracker.is_exhausted("groq", today) is True


def test_unknown_ceiling_is_never_exhausted_by_this_check_alone() -> None:
    # provider-limits.md: Gemini's real ceiling is unpublished/unverified
    # — an unknown limit must not make the provider unusable-by-construction.
    tracker = QuotaTracker(store=InMemoryQuotaStore(), limits={"gemini": ProviderLimits(tpd=None, rpd=None)})
    today = date(2026, 8, 6)
    tracker.record("gemini", tokens=10_000_000, requests=1_000_000, day=today)
    assert tracker.is_exhausted("gemini", today) is False
    status = tracker.status("gemini", today)
    assert status.tokens_remaining is None
    assert status.requests_remaining is None
    assert status.tokens_used_pct is None


def test_usage_accumulates_across_multiple_record_calls_same_day() -> None:
    tracker = QuotaTracker(store=InMemoryQuotaStore())
    today = date(2026, 8, 6)
    tracker.record("groq", tokens=100, requests=1, day=today)
    tracker.record("groq", tokens=250, requests=1, day=today)
    status = tracker.status("groq", today)
    assert status.used == QuotaUsage(tokens=350, requests=2)


def test_usage_does_not_leak_across_days() -> None:
    tracker = QuotaTracker(store=InMemoryQuotaStore())
    day1 = date(2026, 8, 6)
    day2 = day1 + timedelta(days=1)
    tracker.record("groq", tokens=500, requests=5, day=day1)
    assert tracker.status("groq", day2).used == QuotaUsage()
    assert tracker.status("groq", day1).used == QuotaUsage(tokens=500, requests=5)


def test_usage_does_not_leak_across_providers() -> None:
    tracker = QuotaTracker(store=InMemoryQuotaStore())
    today = date(2026, 8, 6)
    tracker.record("groq", tokens=500, requests=5, day=today)
    assert tracker.status("gemini", today).used == QuotaUsage()


def test_all_status_returns_one_row_per_provider_in_order() -> None:
    limits = {"groq": ProviderLimits(tpd=100), "gemini": ProviderLimits()}
    tracker = QuotaTracker(store=InMemoryQuotaStore(), limits=limits)
    today = date(2026, 8, 6)
    tracker.record("groq", tokens=10, day=today)
    rows = tracker.all_status(["groq", "gemini"], today)
    assert [r.provider for r in rows] == ["groq", "gemini"]
    assert rows[0].used.tokens == 10


def test_limits_for_unconfigured_provider_returns_fully_unknown() -> None:
    tracker = QuotaTracker(store=InMemoryQuotaStore(), limits={})
    assert tracker.limits_for("some_new_provider") == ProviderLimits()


# ── default seed values (documented, never hardcoded into logic) ────────


def test_default_limits_groq_matches_verified_provider_limits_doc() -> None:
    # provider-limits.md §1.1, VERIFIED primary source for qwen/qwen3.6-27b
    # (the ADR §9.1-ruled model): 1,000 RPD, 200,000 TPD.
    assert DEFAULT_LIMITS["groq"] == ProviderLimits(tpd=200_000, rpd=1_000)


def test_default_limits_gemini_is_deliberately_unknown() -> None:
    # provider-limits.md §1.2: Google does not publish a static Free-tier
    # ceiling — seeding a specific number here would violate "do not
    # build a hard budget on an UNVERIFIED number."
    assert DEFAULT_LIMITS["gemini"] == ProviderLimits(tpd=None, rpd=None)


# ── DB-backed store + config load/save (real Postgres) ───────────────────

pytestmark = pytest.mark.skipif(not pg_available(), reason="no reachable Postgres (set TEST_DATABASE_URL)")


def test_settings_quota_store_persists_and_accumulates() -> None:
    with migrated_schema() as conn:
        store = SettingsQuotaStore(conn=conn)
        today = date(2026, 8, 6)
        assert store.get("groq", today) == QuotaUsage()

        store.add("groq", today, tokens=500, requests=1)
        conn.commit()
        assert store.get("groq", today) == QuotaUsage(tokens=500, requests=1)

        store.add("groq", today, tokens=250, requests=1)
        conn.commit()
        assert store.get("groq", today) == QuotaUsage(tokens=750, requests=2)


def test_settings_quota_store_is_isolated_per_provider_and_day() -> None:
    with migrated_schema() as conn:
        store = SettingsQuotaStore(conn=conn)
        day1 = date(2026, 8, 6)
        day2 = date(2026, 8, 7)
        store.add("groq", day1, tokens=100, requests=1)
        store.add("gemini", day1, tokens=200, requests=1)
        store.add("groq", day2, tokens=300, requests=1)
        conn.commit()

        assert store.get("groq", day1) == QuotaUsage(tokens=100, requests=1)
        assert store.get("gemini", day1) == QuotaUsage(tokens=200, requests=1)
        assert store.get("groq", day2) == QuotaUsage(tokens=300, requests=1)


def test_load_provider_limits_falls_back_to_default_when_unset() -> None:
    with migrated_schema() as conn:
        assert load_provider_limits(conn) == DEFAULT_LIMITS


def test_load_provider_limits_with_no_conn_returns_default() -> None:
    assert load_provider_limits(None) == DEFAULT_LIMITS


def test_save_and_load_provider_limits_round_trips() -> None:
    with migrated_schema() as conn:
        custom = {"groq": ProviderLimits(tpd=150_000, rpd=800), "gemini": ProviderLimits(tpd=1_000_000, rpd=1500)}
        save_provider_limits(conn, custom)
        conn.commit()

        reloaded = load_provider_limits(conn)
        assert reloaded == custom


def test_quota_tracker_wired_to_real_settings_store_end_to_end() -> None:
    with migrated_schema() as conn:
        save_provider_limits(conn, {"groq": ProviderLimits(tpd=1000, rpd=5)})
        conn.commit()

        tracker = QuotaTracker(store=SettingsQuotaStore(conn=conn), limits=load_provider_limits(conn))
        today = date(2026, 8, 6)
        for _ in range(5):
            tracker.record("groq", tokens=100, requests=1, day=today)
            conn.commit()

        assert tracker.is_exhausted("groq", today) is True  # 5 requests == rpd ceiling
        status = tracker.status("groq", today)
        assert status.used == QuotaUsage(tokens=500, requests=5)
        assert status.requests_remaining == 0
