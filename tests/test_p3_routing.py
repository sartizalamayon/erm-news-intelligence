"""`app.services.llm.routing` — the CONFIG-NOT-CODE feature routing
surface DoD #1 requires: "which provider and model serves each AI
feature... is an admin-settable value read at runtime, never a
hardcoded constant." ADMIN-API builds a UI over this — these tests
prove the read/write round-trip and the ADR §9.1 default (Groq primary,
Gemini secondary, per feature) it degrades to.
"""

from __future__ import annotations

import pytest

from app.db.testing import migrated_schema, pg_available
from app.errors import ValidationError
from app.services.llm.routing import (
    FEATURES,
    SCORING,
    TRANSLATION,
    FeatureRoute,
    ProviderChoice,
    RoutingConfig,
    default_feature_route,
    default_routing_config,
    load_routing_config,
    save_routing_config,
)

pytestmark = pytest.mark.skipif(not pg_available(), reason="no reachable Postgres (set TEST_DATABASE_URL)")


def test_default_routing_config_is_groq_primary_gemini_secondary_for_every_feature() -> None:
    config = default_routing_config()
    assert set(config.routes.keys()) == set(FEATURES)
    for feature in FEATURES:
        route = config.route_for(feature)
        assert route.primary.provider == "groq"
        assert route.secondary is not None
        assert route.secondary.provider == "gemini"
        assert route.primary.model is None  # no override — provider_configs' own model applies
        assert route.secondary.model is None


def test_route_for_unknown_feature_raises_validation_error() -> None:
    config = default_routing_config()
    with pytest.raises(ValidationError):
        config.route_for("not_a_real_feature")


def test_provider_choice_accepts_any_provider_string() -> None:
    # Task 3 (owner requirement, coordinator message): "admins add API
    # keys for arbitrary providers from the dashboard" -- ProviderChoice
    # must not hardcode a provider whitelist. Validity is checked at
    # RESOLUTION time (app.services.llm.registry raises
    # ConfigurationError if a routed provider never resolves to a real
    # account), not at config-authoring time here.
    assert ProviderChoice(provider="mistral").provider == "mistral"
    assert ProviderChoice(provider="some-future-provider").provider == "some-future-provider"


def test_load_routing_config_with_no_conn_returns_default() -> None:
    assert load_routing_config(None) == default_routing_config()


def test_load_routing_config_falls_back_to_default_when_unset() -> None:
    with migrated_schema() as conn:
        config = load_routing_config(conn)
        assert config == default_routing_config()


def test_save_and_load_routing_config_round_trips() -> None:
    with migrated_schema() as conn:
        custom = RoutingConfig(
            routes={
                **default_routing_config().routes,
                SCORING: FeatureRoute(
                    primary=ProviderChoice(provider="gemini", model="gemini-2.5-flash"),
                    secondary=ProviderChoice(provider="groq"),
                ),
            }
        )
        save_routing_config(conn, custom)
        conn.commit()

        reloaded = load_routing_config(conn)
        scoring_route = reloaded.route_for(SCORING)
        assert scoring_route.primary.provider == "gemini"
        assert scoring_route.primary.model == "gemini-2.5-flash"
        assert scoring_route.secondary is not None
        assert scoring_route.secondary.provider == "groq"

        # untouched features still default
        translation_route = reloaded.route_for(TRANSLATION)
        assert translation_route == default_feature_route()


def test_partial_admin_edit_does_not_blow_away_other_features() -> None:
    with migrated_schema() as conn:
        # simulate a partial write: only SCORING is present in the JSON
        import json

        from app.db.settings import set_setting

        set_setting(
            conn,
            "llm_routing",
            json.dumps({SCORING: {"primary": {"provider": "gemini", "model": None}, "secondary": None}}),
        )
        conn.commit()

        config = load_routing_config(conn)
        assert config.route_for(SCORING).primary.provider == "gemini"
        assert config.route_for(SCORING).secondary is None
        # every other feature still gets the ADR §9.1 default
        for feature in FEATURES:
            if feature == SCORING:
                continue
            assert config.route_for(feature) == default_feature_route()


def test_corrupt_json_in_settings_degrades_to_full_default() -> None:
    with migrated_schema() as conn:
        from app.db.settings import set_setting

        set_setting(conn, "llm_routing", "{not valid json")
        conn.commit()

        assert load_routing_config(conn) == default_routing_config()


def test_stored_route_to_a_provider_this_module_never_heard_of_is_preserved() -> None:
    # Task 3: routing.py must accept ANY provider string an admin
    # configures -- including a genuinely new provider this module has
    # no built-in knowledge of. Whether it actually RESOLVES to a real
    # client is app.services.llm.registry's job (ConfigurationError if
    # nothing configured), not routing.py's.
    with migrated_schema() as conn:
        import json

        from app.db.settings import set_setting

        set_setting(
            conn,
            "llm_routing",
            json.dumps({SCORING: {"primary": {"provider": "some-future-provider", "model": None}, "secondary": None}}),
        )
        conn.commit()

        config = load_routing_config(conn)
        assert config.route_for(SCORING).primary.provider == "some-future-provider"
