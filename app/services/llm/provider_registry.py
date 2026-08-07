"""Generic, admin-editable provider registry — the owner's Task 3
requirement: "admins add API keys for arbitrary providers from the
dashboard and assign which provider/model serves which AI workflow."

`provider_configs` (ARCH-DB's `migrations/0006_provider_configs.sql`)
is schema-constrained to exactly `CHECK (provider IN ('groq', 'gemini'))`
and has no `base_url` column — both are exactly what a generic
OpenAI-compatible adapter needs, and neither is available without a
migration this task may not add (`migrations/**` is off-limits). This
module is the additive answer: a `settings`-table-backed
(`0001_settings.sql`, the SAME generic KV store
`app.services.llm.routing`/`.quota` already use) registry of PROVIDER
DEFINITIONS and PROVIDER CREDENTIALS, independent of `provider_configs`'
fixed two-row shape — so a NEW provider (Mistral today; anything else
later) is one admin write, never a schema change or a new Python class.

Two concerns, deliberately separated:

  * `ProviderDefinition` — what KIND of adapter a provider uses and its
    connection shape (`base_url` for OpenAI-compatible providers, ignored
    for Gemini). Non-secret, one row per provider.
  * `ProviderCredential` — an ENCRYPTED API key, keyed by
    `(provider, key_id)`. Deliberately a LIST per provider, not a single
    value: "Two keys exist per provider for Groq and Gemini — per-key
    quota pooling is a legitimate lever if per-account limits bind."
    `app.services.llm.registry.build_client_for_feature` builds a
    `FailoverTransport` candidate list from ALL credentials of the
    resolved provider(s), not just one — see that module and
    `app.services.llm.failover`'s generalized N-candidate design.

`app.services.llm.registry.load_provider_account()` checks
`provider_configs` FIRST for `groq`/`gemini` (ARCH-DB's table stays the
primary source, unchanged) and layers ANY credentials registered here
on top as additional pool members; for any other provider (`mistral`,
future ones), this module is the ONLY source, since `provider_configs`
cannot hold them at all.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from app.db.connection import DictConnection
from app.db.crypto import decrypt_api_key, encrypt_api_key
from app.db.settings import get_setting, set_setting
from app.errors import ValidationError

PROVIDER_DEFINITIONS_SETTINGS_KEY = "llm_provider_definitions"
_CREDENTIALS_SETTINGS_KEY_PREFIX = "llm_provider_credentials"

ADAPTER_OPENAI_COMPATIBLE = "openai_compatible"
ADAPTER_GEMINI = "gemini"
KNOWN_ADAPTERS = (ADAPTER_OPENAI_COMPATIBLE, ADAPTER_GEMINI)


@dataclass(frozen=True)
class ProviderDefinition:
    """What kind of adapter a provider uses and its connection shape.
    `base_url` is REQUIRED for `openai_compatible`, ignored for
    `gemini` (the `google-genai` SDK resolves its own endpoint).
    `enabled=False` means "known, but must never be routed to" — the
    mechanism `cerebras` uses below, rather than deleting the row and
    risking someone re-adding it blind later.
    """

    provider: str
    adapter: str
    base_url: str | None
    default_model: str
    enabled: bool = True
    note: str = ""

    def __post_init__(self) -> None:
        if self.adapter not in KNOWN_ADAPTERS:
            raise ValidationError(f"unknown adapter {self.adapter!r} — must be one of {KNOWN_ADAPTERS}")
        if self.adapter == ADAPTER_OPENAI_COMPATIBLE and not self.base_url:
            raise ValidationError(f"provider {self.provider!r} uses adapter=openai_compatible but has no base_url")


# Documented STARTING POINT only (same pattern as
# `app.services.llm.quota.DEFAULT_LIMITS`) — never consulted directly by
# resolution logic (`app.services.llm.registry` always goes through
# `load_provider_definitions()`), only returned when an admin hasn't
# customized anything yet.
#
#   * groq / gemini — the ADR §9.1-ruled providers. Included here (not
#     just in `provider_configs`) because `provider_configs` has no
#     `base_url` column at all — `app.services.llm.registry` needs THIS
#     entry to know Groq is `openai_compatible` at
#     `https://api.groq.com/openai/v1`, even though the account/model/
#     encrypted-key itself still comes from `provider_configs` first
#     (this module only ADDS pooled extra keys on top — see
#     `app.services.llm.registry._credentials_for_provider`).
#   * mistral — verified live 2026-08-07: `mistral-small-latest` via
#     `https://api.mistral.ai/v1`, OpenAI-compatible, no thinking-token
#     overhead (docs/token-budget.md §2: a real 10-headline judge_batch
#     prompt cost 3,298 total tokens, clean — no `<think>` block, unlike
#     `qwen/qwen3.6-27b` on Groq).
#   * cerebras — verified live 2026-08-07: valid key, but EVERY call
#     returns "Payment required to access this resource" — no free
#     quota exists despite several third-party sources claiming
#     otherwise (the same pattern provider-limits.md §2 already flagged
#     for this exact provider before any live key was available:
#     "Disqualified for this use case — not a renewing free tier, and
#     requires a card"). `enabled=False` records this so nobody retries
#     it blind; flipping it back on is a config change, not a code
#     change, if Cerebras ever adds a free tier.
DEFAULT_PROVIDER_DEFINITIONS: dict[str, ProviderDefinition] = {
    "groq": ProviderDefinition(
        provider="groq",
        adapter=ADAPTER_OPENAI_COMPATIBLE,
        base_url="https://api.groq.com/openai/v1",
        default_model="qwen/qwen3.6-27b",
        enabled=True,
    ),
    "gemini": ProviderDefinition(
        provider="gemini",
        adapter=ADAPTER_GEMINI,
        base_url=None,
        default_model="gemini-2.0-flash",
        enabled=True,
    ),
    "mistral": ProviderDefinition(
        provider="mistral",
        adapter=ADAPTER_OPENAI_COMPATIBLE,
        base_url="https://api.mistral.ai/v1",
        default_model="mistral-small-latest",
        enabled=True,
    ),
    "cerebras": ProviderDefinition(
        provider="cerebras",
        adapter=ADAPTER_OPENAI_COMPATIBLE,
        base_url="https://api.cerebras.ai/v1",
        default_model="gpt-oss-120b",
        enabled=False,
        note=(
            "Verified 2026-08-07 with a real key: every call returns "
            "'Payment required to access this resource' — no free tier "
            "exists. Do not re-enable without a paid plan."
        ),
    ),
}


def _definition_to_json(d: ProviderDefinition) -> dict[str, object]:
    return {
        "adapter": d.adapter,
        "base_url": d.base_url,
        "default_model": d.default_model,
        "enabled": d.enabled,
        "note": d.note,
    }


def _definition_from_json(provider: str, data: dict[str, object]) -> ProviderDefinition:
    return ProviderDefinition(
        provider=provider,
        adapter=str(data.get("adapter", ADAPTER_OPENAI_COMPATIBLE)),
        base_url=data.get("base_url"),  # type: ignore[arg-type]
        default_model=str(data.get("default_model", "")),
        enabled=bool(data.get("enabled", True)),
        note=str(data.get("note", "")),
    )


def load_provider_definitions(conn: DictConnection | None) -> dict[str, ProviderDefinition]:
    """Admin-configured provider definitions, read from
    `settings.llm_provider_definitions`. Falls back to
    `DEFAULT_PROVIDER_DEFINITIONS` when unset or `conn` is None (no DB
    available). Note this registry does NOT include `groq`/`gemini` —
    those are `provider_configs`' territory; `app.services.llm.registry`
    is the module that merges both sources.
    """
    if conn is None:
        return dict(DEFAULT_PROVIDER_DEFINITIONS)
    raw = get_setting(conn, PROVIDER_DEFINITIONS_SETTINGS_KEY, default="")
    if not raw.strip():
        return dict(DEFAULT_PROVIDER_DEFINITIONS)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return dict(DEFAULT_PROVIDER_DEFINITIONS)
    out: dict[str, ProviderDefinition] = {}
    for provider, entry in data.items():
        try:
            out[provider] = _definition_from_json(provider, entry)
        except ValidationError:
            continue  # a corrupt/invalid stored row degrades to "not defined", not a crash
    return out or dict(DEFAULT_PROVIDER_DEFINITIONS)


def save_provider_definitions(conn: DictConnection, definitions: dict[str, ProviderDefinition]) -> None:
    """Admin write path — the intended target of ADMIN-API's future
    "add a provider" form: `provider` (free text), `base_url`,
    `default_model`. Writes the FULL map; callers adding one provider
    should `load_provider_definitions()` first, add/replace one entry,
    and pass the whole dict back here.
    """
    payload = {provider: _definition_to_json(d) for provider, d in definitions.items()}
    set_setting(conn, PROVIDER_DEFINITIONS_SETTINGS_KEY, json.dumps(payload))


@dataclass(frozen=True)
class ProviderCredential:
    """One decrypted API key for one provider, labeled `key_id` (an
    admin-chosen string, e.g. `"primary"`/`"secondary"`/a team name) —
    the unit `app.services.llm.registry` pools across for per-key
    failover (Task 3: "per-key quota pooling is a legitimate lever").
    """

    provider: str
    key_id: str
    api_key: str


def _credentials_key(provider: str) -> str:
    return f"{_CREDENTIALS_SETTINGS_KEY_PREFIX}:{provider}"


def load_provider_credentials(conn: DictConnection, provider: str) -> list[ProviderCredential]:
    """ALL configured keys for `provider`, in admin-entry order.
    Decrypts each via `app.db.crypto` (same Fernet mechanism
    `provider_configs.api_key_ciphertext` uses) — plaintext keys are
    never stored, only ever held in memory for the duration of one
    request, matching ADR §9.1's security requirement.
    """
    raw = get_setting(conn, _credentials_key(provider), default="")
    if not raw.strip():
        return []
    try:
        entries = json.loads(raw)
    except json.JSONDecodeError:
        return []
    out: list[ProviderCredential] = []
    for entry in entries:
        try:
            ciphertext = bytes.fromhex(entry["ciphertext_hex"])
            api_key = decrypt_api_key(ciphertext)
        except Exception:  # noqa: BLE001 - a single corrupt/rotated-key row must not break every other credential
            continue
        out.append(ProviderCredential(provider=provider, key_id=entry.get("key_id", ""), api_key=api_key))
    return out


def save_provider_credential(conn: DictConnection, provider: str, key_id: str, api_key: str) -> None:
    """Admin write path: add or replace ONE key (`key_id`) for
    `provider`. Encrypts before storage — the plaintext `api_key`
    parameter is never itself persisted or logged (only its ciphertext
    and `app.logging.mask()`-style masked suffix would ever be, and
    this function doesn't even return the masked form — callers needing
    it should call `app.db.crypto.encrypt_api_key` themselves for the
    display value, exactly as `provider_configs`' write path is
    documented to do).
    """
    if not key_id.strip():
        raise ValidationError("key_id must be non-empty")
    ciphertext, _masked = encrypt_api_key(api_key)
    # re-encrypting every OTHER existing key is unnecessary — only the
    # new/replaced one changed — so read the raw (already-encrypted)
    # rows and only touch `key_id`'s entry.
    stored = _raw_credential_rows(conn, provider)
    stored[key_id] = ciphertext.hex()
    payload = [{"key_id": k, "ciphertext_hex": v} for k, v in stored.items()]
    set_setting(conn, _credentials_key(provider), json.dumps(payload))


def _raw_credential_rows(conn: DictConnection, provider: str) -> dict[str, str]:
    raw = get_setting(conn, _credentials_key(provider), default="")
    if not raw.strip():
        return {}
    try:
        entries = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return {entry["key_id"]: entry["ciphertext_hex"] for entry in entries if "key_id" in entry}


def remove_provider_credential(conn: DictConnection, provider: str, key_id: str) -> None:
    stored = _raw_credential_rows(conn, provider)
    stored.pop(key_id, None)
    payload = [{"key_id": k, "ciphertext_hex": v} for k, v in stored.items()]
    set_setting(conn, _credentials_key(provider), json.dumps(payload))
