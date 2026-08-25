"""A daily quota ceiling belongs to a MODEL, never to a vendor.

This suite exists because `app.services.llm.quota.DEFAULT_LIMITS` was
keyed by provider alone and held `groq: tpd=200,000 / rpd=1,000` —
numbers sourced, correctly, for `qwen/qwen3.6-27b`. `provider_registry`
had already moved Groq to `llama-3.1-8b-instant` (14,400 RPD, 500,000
TPD, live-confirmed against this project's own keys on 2026-08-14) and
`migrations/0027_retire_seeded_model_ids.sql` had already emptied the
seeded model id from the database. The ceilings never followed, because
nothing structurally tied them to the model they were measured on.

On any database with no admin-configured ceilings — i.e. every fresh
deploy — that cost two things:

  * `app.services.admin.get_quota_status` serves these numbers straight
    to the admin console, so Groq read 100% of budget after 1,000
    requests against a real ceiling 14.4x higher.
  * `app.services.llm.failover.FailoverTransport` consults
    `QuotaStatus.exhausted` to skip a candidate BEFORE spending a call
    on it, so past request 1,000 it would have abandoned a completely
    healthy provider for the rest of the day.

Every assertion below is written against
`provider_registry.DEFAULT_PROVIDER_DEFINITIONS` — the single source of
truth for anything provider-specific (see
`tests/test_provider_model_source_of_truth.py`) — rather than against a
second copy of the model id, so the next model change either carries its
measured ceilings with it or resolves to UNKNOWN, and can never inherit
a retired model's numbers again.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.db.testing import migrated_schema, pg_available
from app.services.llm.provider_registry import DEFAULT_PROVIDER_DEFINITIONS
from app.services.llm.quota import (
    DEFAULT_LIMITS,
    InMemoryQuotaStore,
    ProviderLimits,
    QuotaTracker,
    default_limits_for_model,
    default_provider_limits,
)

# The model the registry currently defaults Groq to, and the ceilings
# read off Groq's own published free-tier table and confirmed live via
# `x-ratelimit-limit-requests: 14400`.
GROQ_LIVE_RPD = 14_400
GROQ_LIVE_TPD = 500_000

# What the retired reasoning model capped at. Named here so a
# regression fails with an explanation instead of a bare inequality.
RETIRED_QWEN_RPD = 1_000
RETIRED_QWEN_TPD = 200_000


def test_groq_default_ceilings_are_the_registrys_current_models_not_the_retired_ones() -> None:
    assert DEFAULT_PROVIDER_DEFINITIONS["groq"].default_model == "llama-3.1-8b-instant", (
        "this suite's numbers were measured for llama-3.1-8b-instant; if the registry "
        "has moved on, measure the new model and add it to quota._MODEL_LIMITS"
    )
    assert DEFAULT_LIMITS["groq"] == ProviderLimits(tpd=GROQ_LIVE_TPD, rpd=GROQ_LIVE_RPD), (
        f"Groq's default ceilings are {DEFAULT_LIMITS['groq']}, not the "
        f"{GROQ_LIVE_TPD:,} TPD / {GROQ_LIVE_RPD:,} RPD live-confirmed for "
        f"{DEFAULT_PROVIDER_DEFINITIONS['groq'].default_model!r}. If this says "
        f"{RETIRED_QWEN_TPD:,}/{RETIRED_QWEN_RPD:,}, the ceilings belong to "
        "qwen/qwen3.6-27b, which 0027 retired."
    )


def test_every_providers_default_ceiling_is_resolved_through_its_registry_model() -> None:
    """The structural property, not the numbers: DEFAULT_LIMITS must be
    exactly what you get by asking the registry which model each
    provider uses and looking that pair up.
    """
    for provider, definition in DEFAULT_PROVIDER_DEFINITIONS.items():
        assert DEFAULT_LIMITS[provider] == default_limits_for_model(provider, definition.default_model), (
            f"{provider}'s default ceilings do not match the ones recorded for its "
            f"registry model {definition.default_model!r} — they were pinned to the "
            "provider instead of to the model, which is exactly how the qwen numbers "
            "outlived qwen."
        )
    assert set(DEFAULT_LIMITS) == set(DEFAULT_PROVIDER_DEFINITIONS)
    assert DEFAULT_LIMITS == default_provider_limits()


def test_an_unmeasured_model_resolves_to_unknown_rather_than_a_neighbours_numbers() -> None:
    """The rot-proofing. When the registry next moves Groq to a model
    nobody has measured, the honest answer is "we do not know", which
    `exhausted` already treats as never-exhausted and leaves the
    provider's own 429 as the signal. Inheriting the previous model's
    ceilings is the failure mode this whole file is about.
    """
    assert default_limits_for_model("groq", "some-model-nobody-has-measured-yet") == ProviderLimits()
    assert default_limits_for_model("a-provider-that-does-not-exist", "whatever") == ProviderLimits()


def test_a_model_that_is_still_pinned_by_an_admin_keeps_its_own_measured_ceilings() -> None:
    """The other half: the retired numbers are not wrong, only
    misfiled. An admin who deliberately re-pins Groq to qwen through the
    console should still get the ceilings measured for qwen.
    """
    assert default_limits_for_model("groq", "qwen/qwen3.6-27b") == ProviderLimits(
        tpd=RETIRED_QWEN_TPD, rpd=RETIRED_QWEN_RPD
    )


def test_failover_does_not_abandon_groq_at_the_retired_models_request_ceiling() -> None:
    """The consequence that actually costs something at run time.

    `FailoverTransport` skips a candidate whose `QuotaStatus.exhausted`
    is True before spending an HTTP call on it. With the retired
    ceiling, request 1,000 of a ~1,100 req/day workload took a healthy
    provider out of rotation for the rest of the day.
    """
    tracker = QuotaTracker(store=InMemoryQuotaStore(), limits=dict(DEFAULT_LIMITS))
    day = date(2026, 8, 25)
    tracker.record("groq", tokens=0, requests=RETIRED_QWEN_RPD, day=day)
    assert not tracker.is_exhausted("groq", day), (
        f"Groq counted as exhausted at {RETIRED_QWEN_RPD:,} requests — the retired "
        f"model's ceiling. The current model allows {GROQ_LIVE_RPD:,}."
    )

    tracker.record("groq", tokens=0, requests=GROQ_LIVE_RPD - RETIRED_QWEN_RPD, day=day)
    assert tracker.is_exhausted("groq", day), "the real ceiling must still stop the run"


@pytest.mark.skipif(not pg_available(), reason="no reachable Postgres (set TEST_DATABASE_URL)")
def test_the_admin_console_serves_the_current_models_ceilings_on_a_fresh_database() -> None:
    """End to end, on the exact schema a first deploy gets: nothing is
    configured, so `get_quota_status` falls through to the defaults —
    which is the path the console actually renders "remaining budget"
    from.
    """
    from app.services import admin as admin_service

    with migrated_schema() as conn:
        statuses = {s["provider"]: s for s in admin_service.get_quota_status(conn)}
    assert statuses["groq"]["rpd"] == GROQ_LIVE_RPD
    assert statuses["groq"]["tpd"] == GROQ_LIVE_TPD
    # Gemini stays deliberately unknown — Google publishes nothing and
    # returns no rate-limit headers, so a number here would be invented.
    assert statuses["gemini"]["rpd"] is None
    assert statuses["gemini"]["tpd"] is None
