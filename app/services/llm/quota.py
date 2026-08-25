"""Per-provider quota tracking (ADR §9.1 / DoD #2): tokens and requests
consumed per provider per day, checked against admin-configured
ceilings, exposed for the admin console ("remaining budget"), and the
signal `app.services.llm.failover` uses to skip a provider before
spending a real HTTP call on it.

**All limits are runtime config, never constants** (DoD #2, restated
from provider-limits.md §7: "Google no longer publishes its free-tier
numbers, and Groq's [numbers], while the best-documented today, [are]
explicitly not guaranteed... rate limits... belong in a config file the
operator can update in five minutes... not in code"). `_MODEL_LIMITS`
below is the ONLY place a number appears in this module, and it exists
purely as a documented, overridable starting point returned when the
admin hasn't set anything yet — never read directly by any call/quota
decision, which always goes through `load_provider_limits()`. It is
keyed by (provider, MODEL), because a daily ceiling belongs to a model
and not to the vendor serving it; the per-provider `DEFAULT_LIMITS` is
derived from it through whichever model
`app.services.llm.provider_registry` currently defaults that provider
to. See that block's own comment for the failure this structure exists
to prevent.

Storage: no dedicated quota table exists yet (this task may not touch
`migrations/**` — see module docstring in `app/services/llm/__init__.py`
for the follow-up this implies). Usage counters are persisted through
the existing generic `settings` key/value table
(`app.db.settings.get_setting`/`set_setting`, `0001_settings.sql`) under
per-provider-per-day keys, JSON-encoded. This is correctness-preserving
(get/set semantics) but not optimal for concurrent increments — fine
here because the only writers are (a) the once-daily batch job, single
process, calls sequential by construction, and (b) the FastAPI process,
which only ever reads for the admin console. `InMemoryQuotaStore` is
also provided (used by default when no DB connection is available, and
by every unit test) implementing the identical `QuotaStore` protocol,
so the DB-backed store is a pure swap-in, not a parallel code path.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from typing import Protocol

from app.db.connection import DictConnection
from app.db.settings import get_setting, set_setting
from app.services.llm.provider_registry import DEFAULT_PROVIDER_DEFINITIONS

PROVIDER_LIMITS_SETTINGS_KEY = "llm_provider_limits"
_QUOTA_USAGE_KEY_PREFIX = "llm_quota_usage"


@dataclass(frozen=True)
class ProviderLimits:
    """A provider's daily ceilings. `None` on either field means
    "unknown/untracked" (provider-limits.md's Gemini situation) rather
    than "unlimited" — `QuotaStatus.exhausted` treats an unknown ceiling
    as never-exhausted (there is nothing to compare against), which is a
    deliberate, documented choice: failover on an UNKNOWN Gemini ceiling
    would mean Gemini can never be used at all, which is worse than
    occasionally over-calling an unverified limit and letting the
    provider's own 429 response be the real signal (caught by
    `app.services.llm.failover` regardless of whether our own count
    agrees).
    """

    tpd: int | None = None  # tokens/day ceiling
    rpd: int | None = None  # requests/day ceiling


@dataclass(frozen=True)
class QuotaUsage:
    tokens: int = 0
    requests: int = 0


@dataclass(frozen=True)
class QuotaStatus:
    provider: str
    day: date
    used: QuotaUsage
    limits: ProviderLimits

    @property
    def tokens_remaining(self) -> int | None:
        if self.limits.tpd is None:
            return None
        return self.limits.tpd - self.used.tokens

    @property
    def requests_remaining(self) -> int | None:
        if self.limits.rpd is None:
            return None
        return self.limits.rpd - self.used.requests

    @property
    def tokens_used_pct(self) -> float | None:
        if not self.limits.tpd:
            return None
        return 100.0 * self.used.tokens / self.limits.tpd

    @property
    def requests_used_pct(self) -> float | None:
        if not self.limits.rpd:
            return None
        return 100.0 * self.used.requests / self.limits.rpd

    @property
    def exhausted(self) -> bool:
        """True once a KNOWN ceiling has been reached or crossed. A
        provider with no configured ceiling on either dimension is never
        considered exhausted by this check alone (see `ProviderLimits`
        docstring) — the provider's own 429 response is still the
        backstop either way.
        """
        if self.limits.tpd is not None and self.used.tokens >= self.limits.tpd:
            return True
        if self.limits.rpd is not None and self.used.requests >= self.limits.rpd:
            return True
        return False


# ── Documented starting points, keyed by MODEL rather than by provider ──
#
# A daily ceiling is a property of the (provider, model) PAIR, never of
# the provider. This map used to be keyed by provider alone, and that
# single modelling mistake is what made it rot:
#
#     "groq": ProviderLimits(tpd=200_000, rpd=1_000)
#
# Those two numbers were sourced, correctly and with a live header
# check, for `qwen/qwen3.6-27b` — the model ADR §9.1 originally ruled
# for Groq. `provider_registry` has since moved Groq to
# `llama-3.1-8b-instant` (see `migrations/0027_retire_seeded_model_ids.sql`
# and DEFAULT_PROVIDER_DEFINITIONS' own header for the full reasoning:
# qwen is a REASONING model whose thinking tokens are pure waste for the
# short JSON verdicts this pipeline asks for, and it caps at 1,000 RPD
# against a ~1,100 req/day workload). The ceilings did not follow the
# model, because nothing structurally tied them to it.
#
# What that cost, on a database with no admin-configured ceilings —
# i.e. every fresh deploy:
#
#   * `app.services.admin.get_quota_status` serves these numbers
#     straight to the admin console, so Groq reads 100% of budget after
#     1,000 requests when the real ceiling is 14,400 — 14.4x — and the
#     console tells the operator to stop using a provider that is
#     nowhere near its limit.
#   * `app.services.llm.failover.FailoverTransport` reads
#     `QuotaStatus.exhausted` to skip a candidate BEFORE spending a
#     call on it. Past 1,000 requests it would have abandoned a
#     perfectly healthy Groq for the rest of the day and pushed the
#     whole workload onto the fallbacks.
#
# The fix is the same shape as 0027's: stop storing an answer that can
# outlive the question it was asked about. Ceilings are recorded here
# against the exact model they were measured on, and the per-provider
# default is RESOLVED from whichever model
# `provider_registry.DEFAULT_PROVIDER_DEFINITIONS` currently defaults
# that provider to — the single source of truth for anything
# provider-specific, and the only place a model id is ever live-probed.
#
# The important property is what happens on the NEXT model change: a
# model with no measured entry below resolves to `ProviderLimits()`,
# i.e. UNKNOWN on both dimensions, not to some other model's numbers.
# Unknown is the safe direction and is already a first-class state here
# (see `ProviderLimits`/`exhausted`): we stop pre-emptively skipping the
# provider and let its own 429 be the signal, which is exactly what
# `app.services.llm.failover` is built to catch. A stale number is the
# dangerous direction, because it is silently believed.
_MODEL_LIMITS: dict[tuple[str, str], ProviderLimits] = {
    # groq / llama-3.1-8b-instant — Groq's own published free-tier table
    # (console.groq.com/docs/rate-limits) confirmed live against this
    # project's real keys on 2026-08-14: RPD=14,400 read exactly off
    # `x-ratelimit-limit-requests`, TPD=500,000 from the published
    # table. Live headers also expose TPM=6,000 (tokens/MINUTE), which
    # this dataclass deliberately does not track — DoD #2 asks for
    # per-DAY ceilings; TPM is a wall-clock PACING constraint, modeled
    # in `docs/token-budget.md` and enforced by
    # `app.services.llm.concurrency`'s limiter instead.
    ("groq", "llama-3.1-8b-instant"): ProviderLimits(tpd=500_000, rpd=14_400),
    # groq / qwen/qwen3.6-27b — RETIRED (0027). Kept deliberately, not
    # deleted: it is the row that makes this map's whole point legible.
    # Same provider, different model, ceilings 14.4x apart. Anyone who
    # re-pins Groq to qwen through the admin console still gets the
    # numbers that were actually measured for it (provider-limits.md
    # §1.1, VERIFIED 2026-08-06, and RPD=1,000 re-confirmed live
    # 2026-08-07 via `x-ratelimit-limit-requests`; TPD=200,000 comes
    # from the published table, never contradicted by a live header).
    ("groq", "qwen/qwen3.6-27b"): ProviderLimits(tpd=200_000, rpd=1_000),
    # Everything else is deliberately absent, which resolves to UNKNOWN:
    #   * gemini — provider-limits.md §1.2: Google publishes no static
    #     free-tier RPM/TPM/RPD, and the 2026-08-07 live probe found
    #     Gemini returns no rate-limit headers at all on a successful
    #     response. Genuinely unverifiable from outside a logged-in AI
    #     Studio dashboard, so seeding a contested secondary-source
    #     number would violate that document's own "do not build a hard
    #     budget on an UNVERIFIED number."
    #   * mistral — verified live 2026-08-07 via response headers, but
    #     only per-MINUTE ones (x-ratelimit-limit-tokens-minute=50000,
    #     x-ratelimit-limit-req-minute=50). No daily ceiling is exposed.
    #   * nvidia — verified live 2026-08-07: zero rate-limit headers on
    #     a successful response. Ceilings must be learned from live
    #     429s, never assumed.
    #   * cerebras — no free tier exists at all (every call returns
    #     "Payment required"), so it is `enabled=False` in the registry
    #     and there is nothing to budget.
}


def default_limits_for_model(provider: str, model: str) -> ProviderLimits:
    """The measured daily ceilings for one (provider, model) pair, or
    fully-unknown limits if nobody has measured that pair. See
    `_MODEL_LIMITS` for why unknown is the safe answer rather than a
    neighbouring model's numbers.
    """
    return _MODEL_LIMITS.get((provider, model), ProviderLimits())


def default_provider_limits() -> dict[str, ProviderLimits]:
    """Starting-point ceilings for every provider the registry defines,
    resolved through each provider's CURRENT default model.

    Never consulted directly by any quota decision — every one of those
    goes through `load_provider_limits()`, which prefers whatever the
    admin has configured. This only answers "what should a database
    nobody has configured yet assume", so that a fresh install has real
    numbers instead of "everything looks unlimited by omission."
    """
    return {
        provider: default_limits_for_model(provider, definition.default_model)
        for provider, definition in DEFAULT_PROVIDER_DEFINITIONS.items()
    }


# Materialized once at import, same as the hand-written dict this
# replaces: `DEFAULT_PROVIDER_DEFINITIONS` is itself a module constant,
# so there is nothing here that can change between calls. The name is
# kept because `load_provider_limits`, `QuotaTracker` and several tests
# already reference it; what changed is where the numbers come from.
DEFAULT_LIMITS: dict[str, ProviderLimits] = default_provider_limits()


class QuotaStore(Protocol):
    def get(self, provider: str, day: date) -> QuotaUsage: ...

    def add(self, provider: str, day: date, *, tokens: int, requests: int) -> QuotaUsage: ...


@dataclass
class InMemoryQuotaStore:
    """Process-lifetime only. Default store for tests and for any
    caller that doesn't hand in a DB connection (e.g. a dry-run CLI
    invocation). Never persists across process restarts — the batch job
    should always construct `SettingsQuotaStore` instead so a provider
    that gets exhausted mid-run stays exhausted if the job is ever
    split across multiple processes in the future.
    """

    _usage: dict[tuple[str, date], QuotaUsage] = field(default_factory=dict)

    def get(self, provider: str, day: date) -> QuotaUsage:
        return self._usage.get((provider, day), QuotaUsage())

    def add(self, provider: str, day: date, *, tokens: int, requests: int) -> QuotaUsage:
        current = self.get(provider, day)
        updated = QuotaUsage(tokens=current.tokens + tokens, requests=current.requests + requests)
        self._usage[(provider, day)] = updated
        return updated


@dataclass
class SettingsQuotaStore:
    """DB-backed `QuotaStore` using the generic `settings` key/value
    table — see module docstring for why (no dedicated quota table
    exists yet; this task's constraints forbid adding one). Key shape:
    `llm_quota_usage:{provider}:{YYYY-MM-DD}` -> `{"tokens": N, "requests": M}`.
    """

    conn: DictConnection

    def _key(self, provider: str, day: date) -> str:
        return f"{_QUOTA_USAGE_KEY_PREFIX}:{provider}:{day.isoformat()}"

    def get(self, provider: str, day: date) -> QuotaUsage:
        raw = get_setting(self.conn, self._key(provider, day), default="")
        if not raw:
            return QuotaUsage()
        try:
            data = json.loads(raw)
            return QuotaUsage(tokens=int(data.get("tokens", 0)), requests=int(data.get("requests", 0)))
        except (json.JSONDecodeError, TypeError, ValueError):
            return QuotaUsage()

    def add(self, provider: str, day: date, *, tokens: int, requests: int) -> QuotaUsage:
        current = self.get(provider, day)
        updated = QuotaUsage(tokens=current.tokens + tokens, requests=current.requests + requests)
        set_setting(
            self.conn,
            self._key(provider, day),
            json.dumps({"tokens": updated.tokens, "requests": updated.requests}),
        )
        return updated


def load_provider_limits(conn: DictConnection | None) -> dict[str, ProviderLimits]:
    """Admin-configured ceilings, read from `settings.llm_provider_limits`
    (JSON: `{"<provider>": {"tpd": <int|null>, "rpd": <int|null>}, ...}`).
    Falls back to `DEFAULT_LIMITS` (documented above) when unset, or when
    `conn` is None (no DB available — e.g. a unit test exercising quota
    logic in isolation).
    """
    if conn is None:
        return dict(DEFAULT_LIMITS)
    raw = get_setting(conn, PROVIDER_LIMITS_SETTINGS_KEY, default="")
    if not raw.strip():
        return dict(DEFAULT_LIMITS)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return dict(DEFAULT_LIMITS)
    out: dict[str, ProviderLimits] = {}
    for provider, limits in data.items():
        out[provider] = ProviderLimits(tpd=limits.get("tpd"), rpd=limits.get("rpd"))
    return out or dict(DEFAULT_LIMITS)


def save_provider_limits(conn: DictConnection, limits: dict[str, ProviderLimits]) -> None:
    """Admin write path (ADMIN-API's future 'configure provider
    ceilings' surface calls this). Persists the FULL map — callers
    should read-modify-write via `load_provider_limits()` first if only
    one provider changed.
    """
    payload = {provider: {"tpd": lim.tpd, "rpd": lim.rpd} for provider, lim in limits.items()}
    set_setting(conn, PROVIDER_LIMITS_SETTINGS_KEY, json.dumps(payload))


class QuotaTracker:
    """The object `app.services.llm.failover` and the (future)
    admin-console read endpoint both use: one `QuotaStore` (usage) plus
    one `provider -> ProviderLimits` map (ceilings), combined into a
    `QuotaStatus` per provider per day.
    """

    def __init__(self, store: QuotaStore, limits: dict[str, ProviderLimits] | None = None) -> None:
        self.store = store
        self.limits = dict(limits) if limits is not None else dict(DEFAULT_LIMITS)

    def limits_for(self, provider: str) -> ProviderLimits:
        return self.limits.get(provider, ProviderLimits())

    def record(self, provider: str, *, tokens: int, requests: int = 1, day: date | None = None) -> QuotaUsage:
        """Called after every real provider call (success OR failure —
        a failed request typically still counts against RPD; `tokens`
        should be 0 for calls that never got a response, since no
        tokens were actually billed).
        """
        return self.store.add(provider, day or date.today(), tokens=tokens, requests=requests)

    def status(self, provider: str, day: date | None = None) -> QuotaStatus:
        resolved_day = day or date.today()
        used = self.store.get(provider, resolved_day)
        return QuotaStatus(provider=provider, day=resolved_day, used=used, limits=self.limits_for(provider))

    def is_exhausted(self, provider: str, day: date | None = None) -> bool:
        return self.status(provider, day).exhausted

    def all_status(self, providers: list[str], day: date | None = None) -> list[QuotaStatus]:
        """For the admin console: one row per provider, in the order
        given.
        """
        return [self.status(provider, day) for provider in providers]
