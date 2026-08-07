"""Feature -> provider routing (DoD #1): "which provider and model
serves each AI feature (scoring, translation, dedup verification, topic
clustering, summarization) is an admin-settable value read at runtime,
never a hardcoded constant." ADMIN-API builds a UI over this next — see
module docstring in `app/services/llm/__init__.py` for the full config
surface this package exposes.

Two separate admin-editable layers, deliberately NOT merged into one:

  1. **Provider accounts** (`provider_configs`, ARCH-DB's
     `migrations/0006_provider_configs.sql`, already built): which
     providers exist ('groq' | 'gemini'), their encrypted API key, and
     ONE default model each. This task does not own that table.
  2. **Feature routing** (this module): which provider serves each of
     the 5 named AI features, primary + secondary, with an OPTIONAL
     per-feature model override layered on top of (1)'s default model.
     Stored as JSON in the generic `settings` key/value table
     (`0001_settings.sql`) under `llm_routing` — the same admin-editable
     KV store `app.services.storage` already uses for the storage
     ceiling, so this needs no new migration.

Why two layers instead of one bigger table: `provider_configs` is
account-scoped ("what's my Groq key, what model do I default to") and
is ARCH-DB's schema to evolve; feature routing is a pure preference
("scoring goes to Groq, translation could go to Gemini instead") that
has no natural need to live next to encrypted credentials, and keeping
it here means the admin can change "who scores today" without touching
key storage at all. `app.services.llm.registry` is what actually
resolves a `(feature, run) -> configured client`, joining these two
config layers plus the decrypted key from `app.db.crypto`.

Every route falls back to ADR §9.1's ruling — Groq primary, Google AI
Studio secondary — per FEATURE independently, so a partially-written or
missing `llm_routing` setting degrades to the correct default rather
than to an error or an unrouted feature.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from app.db.connection import DictConnection
from app.db.settings import get_setting, set_setting
from app.errors import ValidationError

ROUTING_SETTINGS_KEY = "llm_routing"

# The 5 AI features DoD #1 names explicitly. `dedup_verification` is
# `verify_group()` (p3-contract.md §1.10); `summarization` is
# `draft_bullet()` (p3-contract.md §1.8) — the one prompt method that is
# NOT part of `app.services.enrichment`'s P3 stage sequence (it's a
# report-generation-time call), included here anyway because DoD #1
# names it as a routable feature and `app/api/routers/prompts.py`'s stub
# already anticipates P3-LLM wiring "suggest a refinement"
# (`suggest_guidance`) — folded into `summarization`'s route rather than
# given a 6th slot, since both are ad hoc/admin-triggered rather than
# part of the daily batch sequence.
SCORING = "scoring"
TRANSLATION = "translation"
DEDUP_VERIFICATION = "dedup_verification"
TOPIC_CLUSTERING = "topic_clustering"
SUMMARIZATION = "summarization"

FEATURES: tuple[str, ...] = (SCORING, TRANSLATION, DEDUP_VERIFICATION, TOPIC_CLUSTERING, SUMMARIZATION)

# ADR §9.1's originally-ruled two providers. Kept as a documented
# reference set for `default_feature_route()` below, NOT as a
# whitelist: the owner's Task 3 requirement ("admins add API keys for
# arbitrary providers from the dashboard") means `ProviderChoice.provider`
# must accept ANY string an admin registers via
# `app.services.llm.provider_registry` (Mistral today; anything else
# later) — validating "does this provider actually resolve to something
# real" happens at RESOLUTION time in `app.services.llm.registry`
# (`ConfigurationError` if nothing configured), not here at config-authoring
# time. A previous revision of this module rejected any provider
# outside this tuple at `ProviderChoice` construction time; that was
# removed specifically to unblock Task 3 — see
# `tests/test_p3_routing.py::test_provider_choice_accepts_any_provider_string`.
KNOWN_PROVIDERS: tuple[str, ...] = ("groq", "gemini")


@dataclass(frozen=True)
class ProviderChoice:
    """One routing target: a provider key plus an OPTIONAL model
    override. `model=None` means "use whatever the provider's own
    definition/`provider_configs` has configured as the default model"
    (resolved by `app.services.llm.registry`, not here) — this module
    only stores the routing preference, never a live model list or a
    live provider list. `provider` is intentionally UNVALIDATED here
    (see `KNOWN_PROVIDERS` docstring above) — an admin can route a
    feature to any provider key `app.services.llm.provider_registry`
    knows about, including ones this module has never heard of.
    """

    provider: str
    model: str | None = None


@dataclass(frozen=True)
class FeatureRoute:
    primary: ProviderChoice
    secondary: ProviderChoice | None = None


@dataclass(frozen=True)
class RoutingConfig:
    routes: dict[str, FeatureRoute] = field(default_factory=dict)

    def route_for(self, feature: str) -> FeatureRoute:
        if feature not in FEATURES:
            raise ValidationError(f"unknown AI feature {feature!r} — must be one of {FEATURES}")
        return self.routes.get(feature, default_feature_route())


def default_feature_route() -> FeatureRoute:
    """ADR §9.1: "Groq primary (qwen/qwen3.6-27b), Google AI Studio
    secondary" — applied uniformly to every feature unless the admin
    overrides a specific one.
    """
    return FeatureRoute(primary=ProviderChoice(provider="groq"), secondary=ProviderChoice(provider="gemini"))


def default_routing_config() -> RoutingConfig:
    return RoutingConfig(routes={feature: default_feature_route() for feature in FEATURES})


def _choice_from_json(data: dict[str, Any] | None) -> ProviderChoice | None:
    if not data:
        return None
    provider = data.get("provider")
    if not provider:
        return None
    return ProviderChoice(provider=provider, model=data.get("model"))


def _route_from_json(data: dict[str, Any]) -> FeatureRoute:
    primary = _choice_from_json(data.get("primary")) or default_feature_route().primary
    secondary = _choice_from_json(data.get("secondary"))
    return FeatureRoute(primary=primary, secondary=secondary)


def _choice_to_json(choice: ProviderChoice | None) -> dict[str, Any] | None:
    if choice is None:
        return None
    return {"provider": choice.provider, "model": choice.model}


def _route_to_json(route: FeatureRoute) -> dict[str, Any]:
    return {"primary": _choice_to_json(route.primary), "secondary": _choice_to_json(route.secondary)}


def load_routing_config(conn: DictConnection | None) -> RoutingConfig:
    """Reads `settings.llm_routing`. Falls back to
    `default_routing_config()` PER FEATURE (not as an all-or-nothing
    block) — a partial admin edit that only set `scoring` still gets
    ADR §9.1 defaults for the other four, and any parse error or missing
    `conn` (e.g. a unit test) degrades to the full default rather than
    raising.
    """
    if conn is None:
        return default_routing_config()
    raw = get_setting(conn, ROUTING_SETTINGS_KEY, default="")
    data: dict[str, Any] = {}
    if raw.strip():
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            data = {}
    routes = {}
    for feature in FEATURES:
        entry = data.get(feature)
        try:
            routes[feature] = _route_from_json(entry) if entry else default_feature_route()
        except ValidationError:
            # Defensive: `ProviderChoice` itself no longer validates the
            # provider string (Task 3 — arbitrary providers are valid
            # config), so this branch is not reachable via a bad
            # `provider` value today. Kept as a backstop for any OTHER
            # validation `_route_from_json` might raise in the future
            # (e.g. a genuinely malformed stored shape) — one feature's
            # bad data degrades to the safe default rather than taking
            # down routing resolution for every feature.
            routes[feature] = default_feature_route()
    return RoutingConfig(routes=routes)


def save_routing_config(conn: DictConnection, config: RoutingConfig) -> None:
    """Admin write path — the intended target of ADMIN-API's future
    "set routing" endpoint. Writes the FULL 5-feature map; callers
    changing one feature should `load_routing_config()` first, replace
    one entry, and pass the whole config back here.
    """
    payload = {feature: _route_to_json(route) for feature, route in config.routes.items()}
    set_setting(conn, ROUTING_SETTINGS_KEY, json.dumps(payload))
