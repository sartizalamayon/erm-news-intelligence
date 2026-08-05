"""Exception taxonomy for the ERM News Intelligence rebuild.

Design goals (see docs/architecture-decision-record.md, GATE 2 — "zero
unhandled exceptions across the RED-TEAM suite"):

* Per-source fetch failures must be isolable: one broken outlet must
  never abort a whole batch run (speed-techniques.md §5, "per-source
  isolation, no shared-state blast radius" — the legacy app already gets
  this right; this taxonomy is what lets the rebuild keep it).
* External-provider failures (LLM APIs, SMTP, any third-party HTTP
  service) must distinguish *retryable* (rate-limited / transient) from
  *permanent*, so a caller knows whether to back off and retry / fail
  over to a secondary provider, or give up immediately.
* Configuration errors fail fast at process startup — before any batch
  or web request work begins — never silently at request time.
* Validation errors are input problems (a bad API request, a malformed
  CLI arg, an admin form) and map cleanly to a 4xx at the API boundary.

Every concrete exception carries enough structured context (source name,
provider name, retryable flag) that app.logging can attach it without
string-parsing the message.

Ownership is noted per class below so later agents know where their new
subclasses belong. Adding a *subclass* of an existing category does not
require touching this file's design — but if a genuinely new top-level
category is needed, that's an ARCH-REPO-level change (this file), not a
per-agent one, to keep the hierarchy coherent.
"""

from __future__ import annotations


class AppError(Exception):
    """Base class for every application-raised exception. Catch this (not
    bare Exception) at the outermost boundary you control — a batch
    per-source loop, a FastAPI exception handler — so truly unexpected
    exceptions (bugs, not modeled failure modes) still surface loudly
    instead of being swallowed by a too-broad except clause.
    """


class ConfigurationError(AppError):
    """Invalid or missing configuration.

    Raise at startup (see app.config.validate_production_settings) and
    let it propagate — never caught and swallowed. A configuration error
    means the process cannot do its job at all, not that one request or
    one source failed.

    Owner: app.config (this package's shared infra); any agent that adds
    a new required setting extends the startup check, not this class.
    """


class ValidationError(AppError):
    """Bad input from a caller: an API request body, a CLI argument, an
    admin-console form (source config, keyword, prompt guidance, etc.).

    Intended to map to HTTP 422 at the API boundary (see
    app.errors.register_exception_handlers). Owner: whichever router or
    schema module raises it (ADMIN-API, AUTH-OTP, FE-WIRE, ...).
    """


class SourceError(AppError):
    """Base for per-source fetch-layer failures (app/sources/*.py).

    Must NEVER propagate out of a single source's run uncaught — the
    batch entrypoint (app.batch.run_daily) catches SourceError (and
    subclasses) once per source, records a failed/degraded status for
    that source only, and continues with the rest. See
    speed-techniques.md §5 for the "loud-fail guard" and distinct
    stale/out_of_window/failed status precedent this mirrors.

    Owner: P1-FETCH (speed-techniques.md §4's source-plugin agent).
    """

    def __init__(
        self,
        message: str,
        *,
        source: str,
        cause: Exception | None = None,
    ) -> None:
        super().__init__(message)
        self.source = source
        self.cause = cause


class SourceFetchError(SourceError):
    """A single fetch attempt failed: feed discovery (iter_feed_items)
    or an individual article body (fetch_article). Typically transient
    (network, timeout, HTTP 5xx) — the shared retry/backoff helper
    (speed-techniques.md §4 point 2) should absorb most of these before
    they ever reach here.
    """


class SourceConfigError(SourceError):
    """A source is misconfigured — bad URL, missing selector, disabled
    but still scheduled, etc. Distinct from SourceFetchError because
    retrying will not help; it needs a human fix (surfaces on the
    Source health screen — frontend-contract.md §3.10).
    """


class ProviderError(AppError):
    """Base for external-provider failures: LLM APIs (Groq, Google AI
    Studio) or SMTP (OTP email). `retryable` is the load-bearing field —
    callers branch on it rather than on exception subtype where possible,
    since two different providers can fail the same way.

    Owner: P3-LLM (LLM providers) / AUTH-OTP (SMTP).
    """

    def __init__(
        self,
        message: str,
        *,
        provider: str,
        retryable: bool,
        cause: Exception | None = None,
    ) -> None:
        super().__init__(message)
        self.provider = provider
        self.retryable = retryable
        self.cause = cause


class ProviderRateLimitedError(ProviderError):
    """Retryable: rate limit or quota hit (HTTP 429, provider-specific
    quota-exceeded response, etc). Caller should back off (the jittered
    exponential-backoff helper from speed-techniques.md §4 point 2 is
    the intended shared mechanism) and/or fail over to the secondary
    provider (ADR §9.1: Groq primary, Google AI Studio secondary).
    """

    def __init__(
        self,
        message: str,
        *,
        provider: str,
        cause: Exception | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message, provider=provider, retryable=True, cause=cause)
        self.retry_after = retry_after


class ProviderPermanentError(ProviderError):
    """Not retryable: invalid/revoked API key, permanently rejected
    request (e.g. malformed payload the provider will never accept),
    account suspended, etc. Retrying is pointless and wastes budget that
    provider-limits.md shows is already tight.
    """

    def __init__(
        self,
        message: str,
        *,
        provider: str,
        cause: Exception | None = None,
    ) -> None:
        super().__init__(message, provider=provider, retryable=False, cause=cause)
