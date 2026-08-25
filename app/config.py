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
  * groq_* / gemini_* / mistral_* / nvidia_* — ADR §9.1 (Groq
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
from typing import Literal, get_args

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.errors import ConfigurationError

Environment = Literal["development", "test", "staging", "production"]

# The set of email transports app.services.email knows how to call. Named
# as its own alias rather than written inline on the field below so that
# `validate_production_settings` can enumerate it with `get_args()` — see
# `_EMAIL_PROVIDER_REQUIRED_ENV` for why that indirection is load-bearing
# and not decoration.
EmailProvider = Literal["resend", "brevo"]


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
    # NO `port` FIELD — removed, and it should not come back. Nothing in
    # app/ or tests/ ever read `settings.port`: uvicorn is started as
    # `--port $PORT` (render.yaml's startCommand), which reads Render's
    # injected env var directly, and render.yaml's own PORT entry said in
    # as many words that it was "documented for clarity only". A settings
    # field that is authoritative-looking and inert is worse than no field
    # — it invites someone to "fix" a port problem by changing a value
    # that cannot affect anything, and it is exactly the class of
    # undeclared-but-unread config that let the dead Gemini model ship.
    # If a future host ever needs the port in Python, add it back at that
    # moment, with the reader.
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
        '`python -c "from cryptography.fernet import Fernet; '
        'print(Fernet.generate_key().decode())"`.',
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
    #
    # `<provider>_model` defaults are EMPTY on purpose, and must stay
    # that way. `app.services.llm.registry._env_credentials` reads them
    # generically as `getattr(settings, f"{provider}_model")`, and
    # `load_provider_account` prefers that value over
    # `provider_registry.DEFAULT_PROVIDER_DEFINITIONS[...].default_model`
    # — so a non-empty default here SHADOWS the registry, which is the
    # only place model ids are ever live-verified.
    #
    # That shadowing was live, and it was production-only. These two
    # fields used to default to `qwen/qwen3.6-27b` and
    # `gemini-2.0-flash`; `provider_registry` had already moved off both
    # (to `llama-3.1-8b-instant` and `gemini-flash-lite-latest`) and
    # documents `gemini-2.0-flash` as verified-dead — HTTP 404 "no longer
    # available" on every key. render.yaml declares neither `GROQ_MODEL`
    # nor `GEMINI_MODEL`, so production would have taken these defaults
    # while every developer machine took the corrected values from its
    # own `.env`: Gemini is the SECONDARY for all five features, so the
    # entire failover path would have 404'd on first use, and Groq would
    # have run on 1,000 RPD instead of 14,400 against a ~1,100 req/day
    # workload. Nothing would have caught it before the deploy.
    #
    # Add a provider by adding it to `provider_registry`, never by
    # putting its model id here. `nvidia` already has no `_model` field
    # at all, which is the shape to copy.
    groq_api_key: str = Field(default="", repr=False, description="console.groq.com API key (bootstrap/CI fallback).")
    groq_model: str = Field(
        default="",
        description="OPTIONAL override of the model id used for Groq. Blank "
        "(the default) means 'use whatever `app.services.llm.provider_registry."
        "DEFAULT_PROVIDER_DEFINITIONS` says', which is the only live-verified "
        "source of model ids in this codebase. Set this only to pin a "
        "different model than the registry's.",
    )
    gemini_api_key: str = Field(default="", repr=False, description="Google AI Studio API key (bootstrap/CI fallback).")
    gemini_model: str = Field(
        default="",
        description="OPTIONAL override of the model id used for Gemini. Blank "
        "(the default) means 'use the provider registry's', per `groq_model`.",
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
        default="",
        description="OPTIONAL override of the model id used for Mistral. Blank "
        "(the default) means 'use the provider registry's', per `groq_model`.",
    )
    nvidia_api_key: str = Field(
        default="",
        repr=False,
        description="NVIDIA API key (optional bootstrap/CI fallback, parity with "
        "groq/gemini/mistral). Admin-DB-configured routing is P3-LLM's primary "
        "design for this provider; this env var only exists so a bootstrap "
        "key can be supplied the same way as the other three.",
    )
    # NO `llm_provider_primary` / `llm_provider_secondary` FIELDS — removed
    # for the same reason as `port` above, but with a sharper edge: these
    # two were not merely unread, they were WRONG. They said "groq" and
    # "gemini", carrying ADR §9.1's original ruling. Provider routing is
    # decided per-feature in app/services/llm/routing.py, where the
    # hardcoded fallback is Mistral primary / Gemini secondary (measured,
    # and documented there against ADR §9.1's original choice), overridden
    # per-feature by admin-stored config. So a reader who trusted these
    # fields — or an operator who set LLM_PROVIDER_PRIMARY in the Render
    # dashboard expecting it to do something — would have been told the
    # pipeline runs on Groq, silently, with no error and no effect. The
    # same shadowing hazard the `<provider>_model` block above documents at
    # length, in its purest form: config that looks like the source of
    # truth for a decision made somewhere else entirely.

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
    email_provider: EmailProvider = Field(
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


# ── Email transport: what each `email_provider` choice cannot start
# without ────────────────────────────────────────────────────────────
#
# Maps every member of `EmailProvider` to the (env var name, Settings
# field name) pairs that must be non-empty for that transport to be able
# to send a single message. `validate_production_settings` reads only the
# entry for the SELECTED provider, so switching EMAIL_PROVIDER switches
# which secrets are mandatory — the two Resend vars are genuinely inert
# while Brevo is active, and vice versa, and neither should be able to
# fail a deploy it has nothing to do with.
#
# WHAT THIS EXISTS TO PREVENT — the deploy this file shipped without it
# would have been a silent, permanent lockout, not a visible outage:
# `email_provider` defaults to "brevo", render.yaml declares
# BREVO_API_KEY / BREVO_FROM_EMAIL as `sync: false` (i.e. the owner is
# prompted for them, and can leave them blank), and nothing anywhere
# checked. app/services/email.py treats "no key OR no from-address" as
# unconfigured, and `_console_fallback_allowed` hard-disables the console
# fallback in production/staging, so `send_otp_email` raises
# ProviderPermanentError on every call. Meanwhile the ONLY admin account
# (migrations/0009_seed_admin.sql) is seeded with no password_hash, so
# OTP-to-email is the sole way to obtain one. Net effect: the blueprint
# launches, `/health/ready` returns 200, Render reports the deploy live
# and healthy — and nobody, including the owner, can ever log in. There
# is no in-band recovery from that state: fixing it requires going back
# to the Render dashboard and redeploying, having first worked out from
# a generic "Email delivery is not configured" 502 what was wrong.
#
# WHY THIS IS DERIVED FROM THE `Literal` AND NOT HAND-LISTED: the
# exhaustiveness guard immediately below turns "someone added a third
# provider to `EmailProvider` and forgot it here" into an import-time
# crash on a developer's machine, rather than into a provider whose
# required secrets are silently never validated — which is precisely how
# Brevo got here in the first place (it was added as an additive
# owner-ruled switch alongside Resend; the production check simply never
# learned about either).
_EMAIL_PROVIDER_REQUIRED_ENV: dict[str, tuple[tuple[str, str], ...]] = {
    "brevo": (
        ("BREVO_API_KEY", "brevo_api_key"),
        ("BREVO_FROM_EMAIL", "brevo_from_email"),
    ),
    "resend": (
        ("RESEND_API_KEY", "resend_api_key"),
        ("RESEND_FROM_EMAIL", "resend_from_email"),
    ),
}

_UNMAPPED_EMAIL_PROVIDERS = tuple(
    provider for provider in get_args(EmailProvider) if provider not in _EMAIL_PROVIDER_REQUIRED_ENV
)
if _UNMAPPED_EMAIL_PROVIDERS:  # pragma: no cover - a source-level mistake, not a runtime state
    # Deliberately a module-level raise, at import, in every environment.
    # A softer form (log a warning, skip the unknown provider, assert only
    # in tests) would reproduce the exact failure this whole block exists
    # to close: a transport that production believes is configured and
    # that has never once been checked. Adding a provider is a two-line
    # change — the Literal and this dict — and this makes forgetting the
    # second line impossible to ship rather than merely discouraged.
    raise ConfigurationError(
        "EmailProvider gained "
        + ", ".join(sorted(_UNMAPPED_EMAIL_PROVIDERS))
        + " without a matching entry in _EMAIL_PROVIDER_REQUIRED_ENV. Add the env "
        "vars that provider cannot send without, so production startup validates "
        "them — see this constant's comment for the lockout this prevents."
    )


def _is_valid_fernet_key(key: str) -> bool:
    """Whether `key` is something `cryptography.fernet.Fernet` will
    actually accept — 32 url-safe-base64-encoded bytes.

    Imported inside the function on purpose. `app.config` is imported by
    essentially everything (including the batch entrypoint and every
    test module), and `cryptography` is a comparatively heavy import for
    a check that only ever runs once, in production, at startup. Nothing
    else in this module needs it.
    """
    from cryptography.fernet import Fernet

    try:
        Fernet(key.encode("utf-8"))
    except (TypeError, ValueError):
        # binascii.Error (raised for non-base64 input) is itself a
        # ValueError subclass, so both "wrong alphabet" and "right
        # alphabet, wrong length" land here.
        return False
    return True


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
    # Any ONE provider bootstrap key is enough to start: routing resolves
    # primary -> secondary and degrades per-feature rather than failing.
    # Mistral and NVIDIA are included because they are genuinely
    # resolvable from env now (app.services.llm.registry's
    # PROVIDER_CONFIGS_PROVIDERS) — previously only Groq/Gemini were, so
    # an install configured solely with MISTRAL_API_KEY would pass this
    # check today and still have had no usable provider.
    if not any(
        (
            settings.groq_api_key,
            settings.gemini_api_key,
            settings.mistral_api_key,
            settings.nvidia_api_key,
        )
    ):
        missing.append("one of MISTRAL_API_KEY / GEMINI_API_KEY / GROQ_API_KEY / NVIDIA_API_KEY")

    # The transport `email_provider` actually selects must be able to
    # send. See _EMAIL_PROVIDER_REQUIRED_ENV's comment for the full
    # story: a blank key here is not a degraded email feature, it is a
    # permanent, silent lockout of every account including the seeded
    # admin, on a deploy Render reports as healthy. The env var name is
    # named exactly, and the reason it is required is named with it,
    # because the operator reading this message is looking at a Render
    # dashboard with ~12 blank secret fields and needs to know which one.
    for env_name, field_name in _EMAIL_PROVIDER_REQUIRED_ENV[settings.email_provider]:
        if not getattr(settings, field_name, ""):
            missing.append(f"{env_name} (required because EMAIL_PROVIDER={settings.email_provider})")

    if missing:
        raise ConfigurationError("Missing required production configuration: " + ", ".join(missing))

    # SECRET_KEY being non-empty is not the same as SECRET_KEY being
    # usable, and the gap between those two is a production-only,
    # delayed-action failure. `app.db.crypto._fernet` calls
    # `Fernet(key.encode())`, which requires exactly 32 url-safe
    # base64-encoded bytes and raises ValueError on anything else — but
    # nothing calls it at boot. Any other string (a password, a UUID, a
    # truncated paste) therefore starts the process cleanly, passes
    # `/health/ready`, serves traffic for as long as nobody touches
    # provider credentials, and then throws the first time an admin
    # saves an LLM provider API key — surfacing as a 500 in the admin UI,
    # a long way in time and space from the env var that caused it.
    # Checked here, at the same moment as everything else the deploy
    # cannot run without, and answered with the generator command rather
    # than just "invalid": the command is already in `secret_key`'s own
    # field description, so repeating it here costs nothing and saves the
    # operator a trip into the source.
    if not _is_valid_fernet_key(settings.secret_key):
        raise ConfigurationError(
            "SECRET_KEY is not a valid Fernet key (it must be 32 url-safe "
            "base64-encoded bytes). Admin-entered LLM provider API keys are "
            "encrypted at rest with it (ADR §9.1), so an invalid value fails "
            "the first time an admin saves a key, not at startup — which is "
            "why this is checked here. Generate a valid one with: "
            'python -c "from cryptography.fernet import Fernet; '
            'print(Fernet.generate_key().decode())"'
        )


@lru_cache
def get_settings() -> Settings:
    """Process-wide cached settings singleton. Tests that need different
    values should not mutate this — construct `Settings(**overrides)`
    directly, or clear the cache with `get_settings.cache_clear()`.
    """
    return Settings()
