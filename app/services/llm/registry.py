"""The join point: `(feature) -> a configured LLMClient with automatic
failover (and, where configured, per-key pooling) already wired in`.
`app.services.enrichment` (the P3 stage orchestrator) and any future
ADMIN-API "test this provider" action should call
`build_client_for_feature()` — neither should ever construct an
`OpenAICompatibleTransport`/`GeminiTransport` by hand.

Joins four independently admin-editable config layers:

  1. `app.services.llm.routing` — which PROVIDER serves this feature
     (primary + optional secondary), read from `settings.llm_routing`.
  2. `provider_configs` (ARCH-DB's `migrations/0006_provider_configs.sql`)
     — `groq`/`gemini`'s default model + ONE encrypted API key each,
     decrypted here via `app.db.crypto` (never before, never logged).
     Remains the PRIMARY source for these two providers, unchanged —
     this module does not compete with ARCH-DB's ownership of that table.
  3. `app.services.llm.provider_registry` — (a) the ADAPTER SHAPE of
     EVERY provider, including `groq`/`gemini` (base_url, adapter kind
     — needed because `provider_configs` has no `base_url` column at
     all), and (b) any provider `provider_configs` cannot represent
     (`mistral`; any future one — its `CHECK (provider IN ('groq',
     'gemini'))` makes that structurally impossible without a
     migration this task may not add) plus ADDITIONAL pooled API keys
     for ANY provider, including groq/gemini (Task 3: "Two keys exist
     per provider for Groq and Gemini — per-key quota pooling is a
     legitimate lever").
  4. `app.services.llm.quota` — daily ceilings + usage, backing both the
     failover/pooling decision and the admin console's "remaining
     budget" view.

A provider (or one of its pooled keys) with NO usable credential is
silently dropped from the candidate list rather than raising — this is
what lets "primary has no key yet, secondary does" degrade to
secondary-as-primary instead of failing every request, and lets a
provider with 0 configured keys simply contribute 0 candidates. Only
raises `ConfigurationError` if the WHOLE candidate list ends up empty,
since at that point there is nothing this call could possibly do.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.config import get_settings
from app.db.connection import DictConnection
from app.db.crypto import decrypt_api_key
from app.errors import ConfigurationError
from app.services.llm.failover import FailoverTransport
from app.services.llm.provider import LLMClient
from app.services.llm.provider_registry import (
    ADAPTER_GEMINI,
    ADAPTER_OPENAI_COMPATIBLE,
    ProviderDefinition,
    load_provider_credentials,
    load_provider_definitions,
)
from app.services.llm.quota import QuotaTracker, SettingsQuotaStore, load_provider_limits
from app.services.llm.routing import ProviderChoice, load_routing_config
from app.services.llm.transport import GeminiTransport, OpenAICompatibleTransport, Transport

# Providers `provider_configs` (ARCH-DB's table) can represent — its own
# CHECK constraint enforces this same set. Any provider outside this
# tuple is ENTIRELY provider_registry's territory (definition AND every
# credential), since provider_configs cannot hold a row for it at all.
PROVIDER_CONFIGS_PROVIDERS = ("groq", "gemini")


@dataclass(frozen=True)
class ProviderAccount:
    provider: str
    model: str
    api_key: str


def load_provider_account(conn: DictConnection, provider: str) -> ProviderAccount | None:
    """The SINGLE account `provider_configs` (ARCH-DB's table) knows
    about for `provider` — unchanged behavior from before Task 3,
    preserved for backward compatibility and because ARCH-DB's table
    remains the primary/first-checked source for `groq`/`gemini`. Falls
    back to the bootstrap env-var key
    (`app.config.Settings.groq_api_key`/`gemini_api_key`) when no
    encrypted key is stored yet, and further falls back to a synthetic
    row (env key + env model) when `provider_configs` has no row at all.
    Returns `None` — never raises — when the provider is `role='disabled'`,
    outside `PROVIDER_CONFIGS_PROVIDERS`, or no key is available from
    either source: "not configured" is a routine state, not an error.
    """
    if provider not in PROVIDER_CONFIGS_PROVIDERS:
        return None
    row = conn.execute(
        "SELECT provider, model, role, api_key_ciphertext FROM provider_configs WHERE provider = %s",
        (provider,),
    ).fetchone()
    settings = get_settings()
    env_keys = {"groq": settings.groq_api_key, "gemini": settings.gemini_api_key}
    env_models = {"groq": settings.groq_model, "gemini": settings.gemini_model}

    if row is None:
        api_key = env_keys.get(provider, "")
        if not api_key:
            return None
        return ProviderAccount(provider=provider, model=env_models.get(provider, ""), api_key=api_key)

    if row["role"] == "disabled":
        return None

    api_key = ""
    ciphertext = row["api_key_ciphertext"]
    if ciphertext:
        api_key = decrypt_api_key(bytes(ciphertext))
    if not api_key:
        api_key = env_keys.get(provider, "")
    if not api_key:
        return None

    model = row["model"] or env_models.get(provider, "")
    if not model:
        return None
    return ProviderAccount(provider=provider, model=model, api_key=api_key)


def _build_transport(
    definition: ProviderDefinition, model: str, api_key: str, provider_name: str, quota: QuotaTracker
) -> Transport:
    """Dispatches on ADAPTER KIND, not on a per-vendor if/elif chain —
    ANY `openai_compatible` provider (`groq`, `mistral`, and `cerebras`
    if it's ever re-enabled) is `OpenAICompatibleTransport`, parameterized
    purely by `definition.base_url`. Adding a new OpenAI-compatible
    provider needs NO change here — only a new `ProviderDefinition`
    (`app.services.llm.provider_registry`), which is exactly the "config,
    not code" shape Task 3 asks for.
    """
    if definition.adapter == ADAPTER_OPENAI_COMPATIBLE:
        assert definition.base_url is not None  # enforced by ProviderDefinition.__post_init__
        return OpenAICompatibleTransport(
            base_url=definition.base_url, api_key=api_key, model=model, provider_name=provider_name, quota=quota
        )
    if definition.adapter == ADAPTER_GEMINI:
        return GeminiTransport(api_key=api_key, model=model, provider_name=provider_name, quota=quota)
    raise ConfigurationError(f"provider {definition.provider!r} uses an unrecognized adapter {definition.adapter!r}")


def _credentials_for_provider(conn: DictConnection, provider: str) -> list[tuple[str, str]]:
    """ALL usable `(key_id, api_key)` pairs for `provider`, in the order
    they should be tried: `provider_configs`' single account first (if
    `provider` is one it can represent, `key_id="default"`), then any
    ADDITIONAL keys registered in `app.services.llm.provider_registry`
    (pooling — Task 3). For a provider outside `PROVIDER_CONFIGS_PROVIDERS`
    (e.g. `mistral`), this is entirely `provider_registry`'s list.
    """
    pairs: list[tuple[str, str]] = []
    account = load_provider_account(conn, provider)
    if account is not None:
        pairs.append(("default", account.api_key))
    for credential in load_provider_credentials(conn, provider):
        if credential.key_id == "default" and pairs:
            continue  # provider_configs already supplied the "default" slot
        pairs.append((credential.key_id, credential.api_key))
    return pairs


def _model_for_provider(
    conn: DictConnection, provider: str, override: str | None, definition: ProviderDefinition
) -> str:
    if override:
        return override
    account = load_provider_account(conn, provider)
    if account is not None and account.model:
        return account.model
    return definition.default_model


def _resolve_choice(
    conn: DictConnection, choice: ProviderChoice, quota: QuotaTracker
) -> list[tuple[str, Transport, str]]:
    """Expands ONE routed provider choice into its full list of
    `(identity, transport, display_name)` candidates — one per
    configured credential (usually one; more if pooled). `identity` is
    the bare provider name when there is exactly one credential
    (matching every pre-Task-3 test/behavior unchanged) or
    `f"{provider}:{key_id}"` when there is more than one, so each
    pooled key's usage is tracked — and can be exhausted — independently.
    """
    definitions = load_provider_definitions(conn)
    definition = definitions.get(choice.provider)
    if definition is None or not definition.enabled:
        return []

    model = _model_for_provider(conn, choice.provider, choice.model, definition)
    credentials = _credentials_for_provider(conn, choice.provider)
    if not credentials:
        return []

    pooled = len(credentials) > 1
    out: list[tuple[str, Transport, str]] = []
    for key_id, api_key in credentials:
        identity = f"{choice.provider}:{key_id}" if pooled else choice.provider
        # Pooled keys of the SAME provider share that provider's
        # configured ceilings (an admin sets Groq's RPD/TPD once, not
        # once per key) but track USAGE independently — mutate the
        # LIVE `quota.limits` dict (not a separate copy) so this takes
        # effect on the SAME QuotaTracker instance every candidate below
        # is checked against.
        if identity not in quota.limits and choice.provider in quota.limits:
            quota.limits[identity] = quota.limits[choice.provider]
        transport = _build_transport(definition, model, api_key, identity, quota)
        display = f"{choice.provider} · {model}" + (f" ({key_id})" if pooled else "")
        out.append((identity, transport, display))
    return out


def build_client_for_feature(conn: DictConnection, feature: str) -> LLMClient:
    """The single entry point `app.services.enrichment` calls per stage.
    `feature` is one of `app.services.llm.routing.FEATURES`
    (`SCORING`, `TRANSLATION`, `DEDUP_VERIFICATION`, `TOPIC_CLUSTERING`,
    `SUMMARIZATION`).
    """
    routing_config = load_routing_config(conn)
    route = routing_config.route_for(feature)
    quota = QuotaTracker(store=SettingsQuotaStore(conn=conn), limits=load_provider_limits(conn))

    candidates: list[tuple[str, Transport, str]] = []
    for choice in (route.primary, route.secondary):
        if choice is None:
            continue
        candidates.extend(_resolve_choice(conn, choice, quota))

    if not candidates:
        checked = route.primary.provider + (f", {route.secondary.provider}" if route.secondary else "")
        raise ConfigurationError(
            f"no LLM provider is configured for feature {feature!r} (checked: {checked}) — "
            "enter an API key via the admin console or set GROQ_API_KEY/GEMINI_API_KEY"
        )

    transport_candidates = [(identity, transport) for identity, transport, _ in candidates]
    failover = FailoverTransport(quota=quota, candidates=transport_candidates)
    display_name = " -> ".join(display for _, _, display in candidates)
    return LLMClient(transport=failover, name=display_name, conn=conn)
