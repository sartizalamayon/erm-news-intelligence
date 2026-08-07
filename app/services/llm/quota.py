"""Per-provider quota tracking (ADR §9.1 / DoD #2): tokens and requests
consumed per provider per day, checked against admin-configured
ceilings, exposed for the admin console ("remaining budget"), and the
signal `app.services.llm.failover` uses to skip a provider before
spending a real HTTP call on it.

**All limits are runtime config, never constants** (DoD #2, restated
from provider-limits.md §7: "Google no longer publishes its free-tier
numbers, and Groq's [numbers], while the best-documented today, [are]
explicitly not guaranteed... rate limits... belong in a config file the
operator can update in five minutes... not in code"). `DEFAULT_LIMITS`
below is the ONLY place a number appears in this module, and it exists
purely as a documented, overridable starting point returned when the
admin hasn't set anything yet — never read directly by any call/quota
decision, which always goes through `load_provider_limits()`.

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


# Documented starting point ONLY — never consulted directly by quota
# logic, only returned by `load_provider_limits()` when the admin hasn't
# configured anything yet, so a fresh install has a sane default instead
# of "everything looks unlimited by omission."
#   * groq: provider-limits.md §1.1, VERIFIED (primary source,
#     console.groq.com/docs/rate-limits, fetched 2026-08-06) for
#     `qwen/qwen3.6-27b` — the ADR §9.1-ruled model: 1,000 RPD, 200,000 TPD.
#   * gemini: provider-limits.md §1.2 — Google does not publish a
#     static Free-tier RPM/TPM/RPD as of 2026-08-06; deliberately left
#     UNKNOWN (None) rather than seeded with a contested secondary-source
#     number, per that document's own "do not build a hard budget on an
#     UNVERIFIED number" instruction. An admin must set this from a
#     logged-in `aistudio.google.com/rate-limit` read before Gemini's
#     quota tracking means anything beyond raw usage counting.
#
# RE-VERIFIED 2026-08-07 with real credentials
# (`docs/provider-limits-VERIFIED.md`, live response headers): Groq's
# `qwen/qwen3.6-27b` RPD=1,000 confirmed exactly via
# `x-ratelimit-limit-requests`; live headers additionally exposed a
# TPM=8,000 (tokens/MINUTE) ceiling this dataclass does not track
# (DoD #2 asks for per-DAY tracking specifically — TPM is a wall-clock
# PACING constraint, modeled in `docs/token-budget.md` instead of here;
# revisit if per-minute enforcement becomes a real runtime requirement).
# TPD itself (200,000) was not independently re-confirmed by a live
# header (Groq's rate-limit headers exposed RPD/TPM, not TPD) but is
# not contradicted by anything measured live either — still the
# best-available number, cited to the same primary docs source.
# Gemini's `tpd=None`/`rpd=None` is DOUBLY confirmed now: the live probe
# found Gemini returns no rate-limit headers at all on a successful
# response, so RPD/TPM remain genuinely unverifiable from outside a
# logged-in AI Studio dashboard — not just "the docs don't publish it."
DEFAULT_LIMITS: dict[str, ProviderLimits] = {
    "groq": ProviderLimits(tpd=200_000, rpd=1_000),
    "gemini": ProviderLimits(tpd=None, rpd=None),
}


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
