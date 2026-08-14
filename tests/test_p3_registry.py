"""`app.services.llm.registry.build_client_for_feature` — the join
point between routing (`settings.llm_routing`), provider accounts
(`provider_configs` for groq/gemini, `app.services.llm.provider_registry`
for everything else including pooled extra keys), and quota. Real
Postgres (`app.db.testing.migrated_schema`) since this exercises the
actual `provider_configs` schema, not a mock.

`GROQ_API_KEY`/`GEMINI_API_KEY` are never set to anything real here —
`conftest.py` already defaults them to `""` — this suite only ever
constructs transports, never calls `_generate()` on them, so no network
attempt is possible even without the autouse network guard.
"""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

from app.db.crypto import encrypt_api_key
from app.db.testing import migrated_schema, pg_available
from app.errors import ConfigurationError
from app.services.llm.provider_registry import save_provider_credential
from app.services.llm.registry import build_client_for_feature, load_provider_account
from app.services.llm.routing import (
    SCORING,
    TRANSLATION,
    FeatureRoute,
    ProviderChoice,
    RoutingConfig,
    save_routing_config,
)
from app.services.llm.transport import OpenAICompatibleTransport

pytestmark = pytest.mark.skipif(not pg_available(), reason="no reachable Postgres (set TEST_DATABASE_URL)")


def _set_secret_key(monkeypatch: pytest.MonkeyPatch) -> str:
    from app.config import get_settings

    key = Fernet.generate_key().decode()
    monkeypatch.setenv("SECRET_KEY", key)
    get_settings.cache_clear()
    return key


def test_load_provider_account_decrypts_admin_entered_key(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_secret_key(monkeypatch)
    with migrated_schema() as conn:
        ciphertext, _masked = encrypt_api_key("gsk_real_admin_key_123")
        conn.execute(
            "UPDATE provider_configs SET api_key_ciphertext = %s, api_key_last4 = %s WHERE provider = 'groq'",
            (ciphertext, "3456"),
        )
        conn.commit()

        account = load_provider_account(conn, "groq")
        assert account is not None
        assert account.api_key == "gsk_real_admin_key_123"
        assert account.model == "qwen/qwen3.6-27b"  # the seeded default from 0006_provider_configs.sql


def test_load_provider_account_falls_back_to_bootstrap_env_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "env-bootstrap-key")
    from app.config import get_settings

    get_settings.cache_clear()
    with migrated_schema() as conn:
        account = load_provider_account(conn, "groq")
        assert account is not None
        assert account.api_key == "env-bootstrap-key"
    get_settings.cache_clear()


def test_load_provider_account_returns_none_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "env-bootstrap-key")
    from app.config import get_settings

    get_settings.cache_clear()
    with migrated_schema() as conn:
        conn.execute("UPDATE provider_configs SET role = 'disabled' WHERE provider = 'groq'")
        conn.commit()
        assert load_provider_account(conn, "groq") is None
    get_settings.cache_clear()


def test_load_provider_account_returns_none_when_nothing_configured() -> None:
    with migrated_schema() as conn:
        # no ciphertext stored, and conftest.py defaults GROQ_API_KEY to ""
        assert load_provider_account(conn, "groq") is None


def test_load_provider_account_returns_none_for_a_non_provider_configs_provider() -> None:
    # mistral cannot have a provider_configs row at all (that table's own
    # CHECK constraint only allows 'groq'/'gemini') -- confirms
    # app.services.llm.registry doesn't try and fail loudly on it.
    with migrated_schema() as conn:
        assert load_provider_account(conn, "mistral") is None


def test_build_client_for_feature_raises_configuration_error_when_nothing_configured() -> None:
    with migrated_schema() as conn:
        with pytest.raises(ConfigurationError):
            build_client_for_feature(conn, SCORING)


def test_build_client_resolves_primary_only_when_secondary_unconfigured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MISTRAL_API_KEY", "env-bootstrap-key")
    # Blank, not delenv: removing the var lets `.env`'s real key through.
    monkeypatch.setenv("GEMINI_API_KEY", "")
    from app.config import get_settings

    get_settings.cache_clear()
    with migrated_schema() as conn:
        client = build_client_for_feature(conn, SCORING)
        assert len(client.transport.candidates) == 1
        identity, transport = client.transport.candidates[0]
        assert identity == "mistral"
        assert transport.__class__ is OpenAICompatibleTransport
        assert transport.base_url == "https://api.mistral.ai/v1"
        assert "mistral" in client.name
    get_settings.cache_clear()


def test_build_client_for_feature_promotes_secondary_when_primary_unconfigured(monkeypatch: pytest.MonkeyPatch) -> None:
    # Blank, not delenv: removing the var lets `.env`'s real key through.
    # scoring's default primary is groq, secondary mistral.
    monkeypatch.setenv("GROQ_API_KEY", "")
    monkeypatch.setenv("MISTRAL_API_KEY", "env-bootstrap-mistral-key")
    from app.config import get_settings

    get_settings.cache_clear()
    with migrated_schema() as conn:
        # primary (groq) has no key anywhere; secondary (mistral) does
        client = build_client_for_feature(conn, SCORING)
        assert len(client.transport.candidates) == 1
        identity, transport = client.transport.candidates[0]
        assert identity == "mistral"
        assert transport.__class__ is OpenAICompatibleTransport
    get_settings.cache_clear()


def test_build_client_for_feature_wires_both_primary_and_secondary(monkeypatch: pytest.MonkeyPatch) -> None:
    # scoring's default route is groq -> mistral (routing._FEATURE_PRIMARY).
    monkeypatch.setenv("GROQ_API_KEY", "env-groq-key")
    monkeypatch.setenv("MISTRAL_API_KEY", "env-mistral-key")
    from app.config import get_settings

    get_settings.cache_clear()
    with migrated_schema() as conn:
        client = build_client_for_feature(conn, SCORING)
        identities = [identity for identity, _ in client.transport.candidates]
        classes = [transport.__class__ for _, transport in client.transport.candidates]
        assert identities == ["groq", "mistral"]
        # Both groq and mistral are OpenAI-compatible; the Gemini
        # adapter is exercised by the dedup_verification route instead.
        assert classes == [OpenAICompatibleTransport, OpenAICompatibleTransport]
    get_settings.cache_clear()


def test_per_feature_model_override_takes_precedence_over_provider_configs(monkeypatch: pytest.MonkeyPatch) -> None:
    """The documented, available-TODAY workaround for a stale/broken
    seeded model (e.g. `provider_configs`' seeded `gemini-2.0-flash`,
    confirmed dead by docs/provider-limits-VERIFIED.md's live probe):
    an admin can override the model per-feature via `save_routing_config`
    without touching `provider_configs` or `app.config.py` at all.
    """
    monkeypatch.setenv("GEMINI_API_KEY", "env-gemini-key")
    from app.config import get_settings

    get_settings.cache_clear()
    with migrated_schema() as conn:
        config = RoutingConfig(
            routes={
                SCORING: FeatureRoute(primary=ProviderChoice(provider="gemini", model="gemini-flash-lite-latest")),
            }
        )
        save_routing_config(conn, config)
        conn.commit()

        client = build_client_for_feature(conn, SCORING)
        _, transport = client.transport.candidates[0]
        assert transport.model == "gemini-flash-lite-latest"
    get_settings.cache_clear()


# ── Task 3: Mistral (a provider `provider_configs` cannot represent) ────


def test_mistral_resolves_entirely_through_provider_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_secret_key(monkeypatch)
    with migrated_schema() as conn:
        save_provider_credential(conn, "mistral", "primary", "mistral-real-key-123")
        conn.commit()

        config = RoutingConfig(routes={TRANSLATION: FeatureRoute(primary=ProviderChoice(provider="mistral"))})
        save_routing_config(conn, config)
        conn.commit()

        client = build_client_for_feature(conn, TRANSLATION)
        identity, transport = client.transport.candidates[0]
        assert identity == "mistral"
        assert transport.__class__ is OpenAICompatibleTransport
        assert transport.base_url == "https://api.mistral.ai/v1"
        assert transport.model == "mistral-small-latest"  # provider_registry's default_model
        assert transport.api_key == "mistral-real-key-123"


def test_nvidia_resolves_entirely_through_provider_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_secret_key(monkeypatch)
    with migrated_schema() as conn:
        save_provider_credential(conn, "nvidia", "primary", "nvidia-real-key-123")
        config = RoutingConfig(routes={TRANSLATION: FeatureRoute(primary=ProviderChoice(provider="nvidia"))})
        save_routing_config(conn, config)
        conn.commit()

        client = build_client_for_feature(conn, TRANSLATION)
        identity, transport = client.transport.candidates[0]
        assert identity == "nvidia"
        assert transport.__class__ is OpenAICompatibleTransport
        assert transport.base_url == "https://integrate.api.nvidia.com/v1"
        assert transport.model == "meta/llama-3.1-8b-instruct"


def test_mistral_per_feature_model_override(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_secret_key(monkeypatch)
    with migrated_schema() as conn:
        save_provider_credential(conn, "mistral", "primary", "mistral-real-key-123")
        config = RoutingConfig(
            routes={TRANSLATION: FeatureRoute(primary=ProviderChoice(provider="mistral", model="mistral-large-latest"))}
        )
        save_routing_config(conn, config)
        conn.commit()

        client = build_client_for_feature(conn, TRANSLATION)
        _, transport = client.transport.candidates[0]
        assert transport.model == "mistral-large-latest"


def test_cerebras_never_resolves_even_if_routed_to_it_and_credentialed(monkeypatch: pytest.MonkeyPatch) -> None:
    # provider_registry.DEFAULT_PROVIDER_DEFINITIONS marks cerebras
    # enabled=False (verified-unavailable, docs/token-budget.md /
    # provider_registry.py) -- routing to it, even WITH a valid-looking
    # credential on file, must resolve to zero candidates, not a
    # transport that will just fail with "payment required" every call.
    _set_secret_key(monkeypatch)
    with migrated_schema() as conn:
        save_provider_credential(conn, "cerebras", "primary", "cerebras-key-that-has-no-quota")
        config = RoutingConfig(routes={TRANSLATION: FeatureRoute(primary=ProviderChoice(provider="cerebras"))})
        save_routing_config(conn, config)
        conn.commit()

        with pytest.raises(ConfigurationError):
            build_client_for_feature(conn, TRANSLATION)


# ── Task 3: per-key pooling ──────────────────────────────────────────────


def test_two_groq_keys_pool_as_two_independent_candidates(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_secret_key(monkeypatch)
    monkeypatch.setenv("GROQ_API_KEY", "env-groq-key-one")
    from app.config import get_settings

    get_settings.cache_clear()
    with migrated_schema() as conn:
        # env var supplies the "default" credential; provider_registry
        # supplies a SECOND, pooled one -- exactly the "two keys exist
        # per provider" scenario.
        # Pin scoring to Groq explicitly: this test is about Groq key
        # POOLING mechanics, not about which provider happens to be the
        # default route (that default is Mistral now — see
        # routing.default_feature_route).
        save_routing_config(
            conn,
            RoutingConfig(
                routes={SCORING: FeatureRoute(primary=ProviderChoice(provider="groq"))}
            ),
        )
        save_provider_credential(conn, "groq", "second", "groq-key-two")
        conn.commit()

        client = build_client_for_feature(conn, SCORING)
        identities = [identity for identity, _ in client.transport.candidates]
        keys = [transport.api_key for _, transport in client.transport.candidates]
        assert identities == ["groq:default", "groq:second"]
        assert keys == ["env-groq-key-one", "groq-key-two"]
    get_settings.cache_clear()


def test_pooled_keys_share_the_providers_configured_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each pooled key gets an INDEPENDENT quota bucket (so 2 keys really
    do give ~2x headroom, not a shared bucket) but both start from the
    SAME configured ProviderLimits for that provider — an admin
    configures Groq's RPD/TPD once, not once per key.
    """
    _set_secret_key(monkeypatch)
    monkeypatch.setenv("GROQ_API_KEY", "env-groq-key-one")
    from app.config import get_settings

    get_settings.cache_clear()
    with migrated_schema() as conn:
        # Pin scoring to Groq explicitly: this test is about Groq key
        # POOLING mechanics, not about which provider happens to be the
        # default route (that default is Mistral now — see
        # routing.default_feature_route).
        save_routing_config(
            conn,
            RoutingConfig(
                routes={SCORING: FeatureRoute(primary=ProviderChoice(provider="groq"))}
            ),
        )
        save_provider_credential(conn, "groq", "second", "groq-key-two")
        conn.commit()

        client = build_client_for_feature(conn, SCORING)
        failover = client.transport
        limits_default = failover.quota.limits_for("groq:default")
        limits_second = failover.quota.limits_for("groq:second")
        assert limits_default == limits_second
        assert limits_default.rpd == 1000  # DEFAULT_LIMITS["groq"], unchanged
    get_settings.cache_clear()
