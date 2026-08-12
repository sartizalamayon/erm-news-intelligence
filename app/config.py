"""Single typed, environment-variable-driven settings module.

Every later agent reads configuration through `get_settings()` — nowhere
else should call `os.environ` directly for application config (CI/test
env vars used only inside tests are fine).

No secrets are committed. `.env.example` at the repo root documents every
variable below with a comment; a real `.env` (gitignored) or the hosting
platform's own env vars (GitHub Actions secrets, Render environment
variables) supply real values at runtime.

Field groups map 1:1 to ADR decisions — nothing here is speculative:
  * database_url            — ADR §1, changed Neon -> Supabase Postgres in §9.4.
  * groq_* / gemini_* / mistral_* / llm_provider_* — ADR §9.1 (Groq
    primary, Google AI Studio secondary, ruled; Mistral added as a
    third, no-thinking-tax provider once live-verified working — see
    provider-limits-VERIFIED.md's "Round 2"). These env vars are the
    bootstrap/CI/local fallback only — ADR §9.1 also rules that
    admin-entered keys are stored encrypted in the database
    (ARCH-DB/ADMIN-API's territory); `secret_key` below is the
    encryption key for that, not for these.
  * resend_*                — ADR §9.3 (OTP-only auth). Resend was
    approved as the OTP email transport (see app/services/email.py);
    it is called over its REST API via `requests`. There is no SMTP
    server anywhere in this system — no smtp_* fields exist here on
    purpose (a prior revision provisioned four unused ones "in case a
    relay is ever added"; removed as dead weight that recreated the
    exact naming confusion the resend_* rename fixed).
  * trusted_proxy_hops      — RT-5 remediation (docs/red-team-findings.md).
    Security-critical, not a tuning knob: default 0 means
    X-Forwarded-For is never trusted. See the field's own description.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.errors import ConfigurationError

Environment = Literal["development", "test", "staging", "production"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ── App / runtime ────────────────────────────────────────────────
    app_env: Environment = Field(
        default="development",
        description="development | test | staging | production. Gates the "
        "production-only fail-fast checks in validate_production_settings().",
    )
    log_level: str = Field(default="INFO", description="DEBUG | INFO | WARNING | ERROR")
    port: int = Field(default=8000, description="HTTP port for uvicorn. Render injects its own PORT.")
    trusted_proxy_hops: int = Field(
        default=0,
        description="SECURITY-CRITICAL, NOT A TUNING KNOB. Number of "
        "reverse-proxy hops between the real client and this process "
        "that are trusted to have each appended their own observed peer "
        "address onto X-Forwarded-For (RFC 7239 'rightmost trusted hop' "
        "model). Default 0 = trust nothing: X-Forwarded-For is never "
        "consulted and the direct socket peer is used — this is the "
        "RT-5 remediation (docs/red-team-findings.md); setting this "
        "higher than the real number of trusted hops in front of this "
        "process reopens the per-IP OTP rate-limit bypass RT-5 found. "
        "Render alone counts as 1 hop; an additional CDN/WAF in front "
        "adds more. Any unparsable or negative value resolves to 0 "
        "(fail safe), never an error or a trusted default. Read by "
        "app.api.routers.auth._trusted_proxy_hops().",
    )
    secret_key: str = Field(
        default="",
        repr=False,
        description="Symmetric (Fernet) key used to encrypt admin-entered "
        "LLM provider API keys at rest, per ADR §9.1's security "
        "requirement. Owned operationally by ARCH-DB; generate with "
        "`python -c \"from cryptography.fernet import Fernet; "
        "print(Fernet.generate_key().decode())\"`.",
    )

    # ── Database (Supabase Postgres — ADR §1 / §9.4) ────────────────
    database_url: str = Field(
        default="",
        repr=False,
        description="Postgres connection string for Supabase. Required in "
        "production (see validate_production_settings); blank is fine for "
        "local dev / CI until ARCH-DB's fixture DB exists. Schema and "
        "connection management are ARCH-DB's territory, not this module's.",
    )

    # ── LLM providers (ADR §9.1, ruled: Groq primary, Google AI Studio
    # secondary; multi-provider with admin-configurable routing) ──────
    groq_api_key: str = Field(default="", repr=False, description="console.groq.com API key (bootstrap/CI fallback).")
    groq_model: str = Field(default="qwen/qwen3.6-27b", description="Ruled model per ADR §9.1.")
    gemini_api_key: str = Field(default="", repr=False, description="Google AI Studio API key (bootstrap/CI fallback).")
    gemini_model: str = Field(
        default="gemini-2.0-flash",
        description="Placeholder pending P3-LLM confirming the exact model id against provider-limits.md.",
    )
    mistral_api_key: str = Field(
        default="",
        repr=False,
        description="Mistral API key (bootstrap/CI fallback). Live-verified working, "
        "50,000 TPM / 50 RPM, no reasoning-token overhead — see "
        "provider-limits-VERIFIED.md's 'Round 2'. No daily (TPD/RPD) ceiling was "
        "exposed by the API; treat it as unknown, never assume unlimited.",
    )
    mistral_model: str = Field(
        default="mistral-small-latest", description="Live-verified working model per provider-limits-VERIFIED.md."
    )
    nvidia_api_key: str = Field(
        default="",
        repr=False,
        description="NVIDIA API key (optional bootstrap/CI fallback, parity with "
        "groq/gemini/mistral). Admin-DB-configured routing is P3-LLM's primary "
        "design for this provider; this env var only exists so a bootstrap "
        "key can be supplied the same way as the other three.",
    )
    llm_provider_primary: str = Field(default="groq", description="ADR §9.1.")
    llm_provider_secondary: str = Field(default="gemini", description="ADR §9.1.")

    # ── Resend (OTP email transport — ADR §9.3, ruled: OTP-only auth) ─
    # Resend is called over its REST API via `requests`
    # (app/services/email.py) — there is no SMTP server in this system,
    # and no smtp_* fields are provisioned here on purpose (see this
    # module's docstring).
    resend_api_key: str = Field(
        default="", repr=False, description="Resend API key, sent as a Bearer token. See app.services.email."
    )
    resend_from_email: str = Field(
        default="", description="Verified Resend sender address OTP emails are sent from. See app.services.email."
    )

    otp_console_fallback_enabled: bool = Field(
        default=True,
        description="When True AND app_env is NOT production/staging, an "
        "unconfigured provider falls back to logging the OTP code to the "
        "server console (WARNING level) instead of failing the request -- "
        "lets local/dev/CI login work with no mail provider set up. ALWAYS "
        "forced off in production/staging regardless of this value -- see "
        "app.services.email.send_otp_email(). Set to False to disable the "
        "fallback even in dev (e.g. to test the real failure path).",
    )

    # ── Brevo (ADDITIVE -- AUTH-OTP's owner-ruled provider switch. See
    # app/services/email.py's module docstring for the full story: Resend
    # needs a verified sending domain grameenphone.com does not have and
    # the owner has ruled out obtaining; Brevo was live-verified instead
    # -- two real emails delivered to a @grameenphone.com inbox (landed
    # in Junk, an accepted/known risk given the sender is a freemail
    # address, not something to "fix" here). `email_provider` is the
    # single switch app.services.email reads; Resend stays fully working
    # as the non-default alternative, nothing above this section is
    # touched.) ─────────────────────────────────────────────────────────
    email_provider: Literal["resend", "brevo"] = Field(
        default="brevo",
        description="Which transport app.services.email.send_otp_email "
        "calls. 'brevo' is the default -- see this field group's header "
        "comment for why. Set to 'resend' to switch back (e.g. once a "
        "verified sending domain exists).",
    )
    brevo_api_key: str = Field(
        default="",
        repr=False,
        description="Brevo API key, sent as the 'api-key' header (NOT an "
        "Authorization: Bearer token -- Brevo's own convention, verified "
        "live). See app.services.email.",
    )
    brevo_from_email: str = Field(
        default="",
        description="Brevo sender address OTP emails are sent from. A "
        "freemail address is expected here (Brevo's dashboard itself "
        "flags this as non-compliant with Google/Yahoo/Microsoft sender "
        "rules) -- deliverability risk is accepted, not solved; see "
        "app.services.email's module docstring.",
    )
    brevo_from_name: str = Field(default="ERM", description="Brevo sender display name. See app.services.email.")

    # ── Frontend / CORS ───────────────────────────────────────────────
    cors_allow_origins: str = Field(
        default="",
        description="Comma-separated list of allowed frontend origins, "
        "e.g. 'https://erm-frontend.onrender.com'. Empty = none allowed.",
    )

    @property
    def cors_allow_origins_list(self) -> list[str]:
        """Parsed view of cors_allow_origins for FastAPI's CORSMiddleware."""
        return [origin.strip() for origin in self.cors_allow_origins.split(",") if origin.strip()]

    # ── Hardening (HARDEN mission, docs/security.md) ─────────────────
    # Additive only — see this module's docstring. Read by
    # app.middleware.* and app.main.create_app(); nothing above this
    # section is touched.
    rate_limit_requests_per_window: int = Field(
        default=120,
        description="General, whole-API rate limit: max requests per "
        "client IP per rate_limit_window_seconds. In-process only (no "
        "Redis, GATE 0) — holds per Render instance, not globally across "
        "instances. Separate from, and in addition to, "
        "app.services.auth's OTP-specific per-email/per-IP limiter.",
    )
    rate_limit_window_seconds: int = Field(
        default=60, description="Sliding window size, in seconds, for rate_limit_requests_per_window."
    )
    max_request_body_bytes: int = Field(
        default=1_000_000,
        description="Hard cap on any request body this API will read, "
        "in bytes. Requests over this are rejected with 413 before the "
        "body is parsed. 1MB is generous for every current admin-form "
        "payload (keywords, prompts, source config) with headroom.",
    )
    max_query_param_length: int = Field(
        default=2000,
        description="Max character length for any single query-string "
        "parameter value (e.g. GET /search's future 'q='). Protects the "
        "GIN-indexed search query from an unbounded free-text input.",
    )

    # ── Dashboard macro indicators (MACRO-DASHBOARD, additive) ────────
    # app.services.macro.fetch_brent / app.api.routers.dashboard's
    # long-term "trend cards". Taka/USD (open.er-api.com) and inflation
    # (World Bank) are both free and keyless, so they need no setting
    # here. Brent's free tier (oilpriceapi.com, 200 req/month, no
    # credit card) does require a per-account API key.
    oilpriceapi_key: str = Field(
        default="",
        repr=False,
        description="oilpriceapi.com API key for the dashboard's Brent "
        "crude trend card (app.services.macro.fetch_brent). Sign up free "
        "(no credit card) at https://www.oilpriceapi.com/auth/signup and "
        "set this as env var OILPRICEAPI_KEY. Blank = the Brent card "
        "shows a clear 'not configured' state rather than a fake zero — "
        "see app.api.routers.dashboard.dashboard_trends.",
    )

    @field_validator("trusted_proxy_hops", mode="before")
    @classmethod
    def _fail_safe_trusted_proxy_hops(cls, value: object) -> int:
        """Replicates, exactly, the fail-safe parsing
        app.api.routers.auth._trusted_proxy_hops() used to implement
        directly against `os.environ` before this became a typed
        setting: any unparsable or negative value resolves to 0 (trust
        nothing) rather than raising or trusting a garbage value. This
        field is security-critical (RT-5) — malformed config must fail
        safe, not fail loud and not fail open.
        """
        if isinstance(value, int):
            return value if value > 0 else 0
        try:
            hops = int(str(value).strip())
        except (TypeError, ValueError):
            return 0
        return hops if hops > 0 else 0


def validate_production_settings(settings: Settings) -> None:
    """Fail fast at process startup when required configuration is
    missing in production. Deliberately NOT a pydantic validator — those
    run on every Settings() construction, which would break tests and
    local dev that build partial Settings on purpose. Call this
    explicitly once, from app.main.create_app() and
    app.batch.run_daily.main(), after the object is built.

    Raises ConfigurationError (never a bare exception) so the taxonomy
    in app.errors stays the single source of truth for "what kind of
    failure is this."
    """
    if settings.app_env != "production":
        return

    missing = [
        name
        for name, value in {
            "DATABASE_URL": settings.database_url,
            "SECRET_KEY": settings.secret_key,
        }.items()
        if not value
    ]
    if not settings.groq_api_key and not settings.gemini_api_key:
        missing.append("GROQ_API_KEY or GEMINI_API_KEY (at least one)")

    if missing:
        raise ConfigurationError(
            "Missing required production configuration: " + ", ".join(missing)
        )


@lru_cache
def get_settings() -> Settings:
    """Process-wide cached settings singleton. Tests that need different
    values should not mutate this — construct `Settings(**overrides)`
    directly, or clear the cache with `get_settings.cache_clear()`.
    """
    return Settings()
