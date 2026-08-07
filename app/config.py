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
