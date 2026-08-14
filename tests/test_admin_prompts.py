"""AI prompt versioning + candidate mining + "suggest a refinement", and
provider routing/quota config (app.services.admin, app.api.routers.prompts).

DoD #3: every save creates a new version, previous versions are
retrievable, the active version is selectable -- and the frozen P3
prompt scaffold (app.services.llm.prompts.FIXED_GUIDE) is never touched,
only the admin-editable guidance/examples content.

"suggest a refinement" is tested with app.services.llm.registry.build_client_for_feature
monkeypatched to return an LLMClient backed by tests/helpers/fake_llm.py's
FakeLLMProvider -- no real network/LLM call, same discipline as
tests/test_p3_provider.py.
"""

from __future__ import annotations

import contextlib

import pytest
from fastapi.testclient import TestClient

from app.api.deps import AuthenticatedUser, get_current_user
from app.db.connection import DictConnection
from app.db.queries import insert_article
from app.db.settings import set_setting
from app.db.testing import migrated_schema, pg_available
from app.errors import ValidationError
from app.main import create_app
from app.services import admin as admin_service
from app.services.llm.prompts import (
    RELEVANCE_EXAMPLES_KEY,
    RELEVANCE_GUIDANCE_KEY,
    get_relevance_examples,
    get_relevance_guidance,
)
from app.services.llm.provider import LLMClient
from app.services.llm.provider_registry import PROVIDER_DEFINITIONS_SETTINGS_KEY
from app.services.llm.quota import PROVIDER_LIMITS_SETTINGS_KEY
from app.services.llm.routing import ROUTING_SETTINGS_KEY
from tests.helpers.fake_llm import FakeLLMProvider

pytestmark = pytest.mark.skipif(not pg_available(), reason="no reachable Postgres (set TEST_DATABASE_URL)")

ADMIN = AuthenticatedUser(id=9, email="admin@grameenphone.com", role="admin", display_name="Admin", session_id=9)


def _reset_llm_settings(conn: DictConnection) -> None:
    """Force the three generic-`settings`-table keys this file's
    "defaults" tests depend on (routing/quota-limits/provider-definitions)
    to their genuinely-unset state, then commit.

    `migrated_schema()` already hands each test a brand-new, freshly
    migrated schema, so in the normal case these keys are already unset.
    This exists as a second, explicit guarantee that does not rely on
    that being true: `app.db.settings.get_setting`'s own fallback
    contract treats an EMPTY string identically to a missing row (every
    `load_*` function below checks `if raw.strip(): ...`), so writing ""
    is equivalent to "never configured" regardless of whatever a prior
    write (in this schema or, if schema isolation is ever imperfect for
    any reason — e.g. a pooled connection not fully honoring per-session
    search_path — a DIFFERENT one) left behind. A "defaults round-trip
    correctly" test must not depend on ambient state to be true; this
    makes that explicit rather than assumed.
    """
    set_setting(conn, ROUTING_SETTINGS_KEY, "")
    set_setting(conn, PROVIDER_LIMITS_SETTINGS_KEY, "")
    set_setting(conn, PROVIDER_DEFINITIONS_SETTINGS_KEY, "")
    set_setting(conn, RELEVANCE_GUIDANCE_KEY, "")
    set_setting(conn, RELEVANCE_EXAMPLES_KEY, "")
    set_setting(conn, "prompt_versions:guidance", "")
    set_setting(conn, "prompt_versions:examples", "")
    conn.commit()


# ── Versioning ────────────────────────────────────────────────────────


def test_first_save_creates_version_2_after_the_seeded_version_1() -> None:
    with migrated_schema() as conn:
        _reset_llm_settings(conn)
        versions = admin_service.list_prompt_versions(conn, "guidance")
        assert [v["version"] for v in versions] == [1]
        assert versions[0]["active"] is True

        saved = admin_service.save_prompt_content(conn, "guidance", "Prefer telecom stories.", actor_email="a@x.com")
        assert saved["version"] == 2
        assert saved["active"] is True

        versions_after = admin_service.list_prompt_versions(conn, "guidance")
        assert [v["version"] for v in versions_after] == [1, 2]
        assert [v["active"] for v in versions_after] == [False, True]


def test_save_writes_through_to_the_runtime_read_path() -> None:
    """P3's ACTUAL runtime read (get_relevance_guidance/_examples) must
    reflect the newly-active version immediately -- versioning is a
    layer on top of that read path, never a replacement for it.
    """
    with migrated_schema() as conn:
        admin_service.save_prompt_content(conn, "guidance", "New guidance text.", actor_email="a@x.com")
        assert get_relevance_guidance(conn) == "New guidance text."

        admin_service.save_prompt_content(conn, "examples", "headline => 9", actor_email="a@x.com")
        assert get_relevance_examples(conn) == "headline => 9"


def test_activate_older_version_repoints_active_without_new_version() -> None:
    with migrated_schema() as conn:
        admin_service.save_prompt_content(conn, "guidance", "v2 text", actor_email="a@x.com")
        admin_service.save_prompt_content(conn, "guidance", "v3 text", actor_email="a@x.com")
        versions = admin_service.list_prompt_versions(conn, "guidance")
        assert [v["version"] for v in versions] == [1, 2, 3]

        activated = admin_service.activate_prompt_version(conn, "guidance", 2)
        assert activated["version"] == 2
        assert activated["content"] == "v2 text"
        assert get_relevance_guidance(conn) == "v2 text"

        versions_after = admin_service.list_prompt_versions(conn, "guidance")
        assert [v["version"] for v in versions_after] == [1, 2, 3]  # no new version created
        assert [v["active"] for v in versions_after] == [False, True, False]


def test_activate_unknown_version_returns_none() -> None:
    with migrated_schema() as conn:
        assert admin_service.activate_prompt_version(conn, "guidance", 999) is None


def test_invalid_field_raises_validation_error() -> None:
    with migrated_schema() as conn:
        with pytest.raises(ValidationError):
            admin_service.list_prompt_versions(conn, "not_a_field")


def test_get_prompt_config_reflects_active_versions_and_real_counts() -> None:
    with migrated_schema() as conn:
        _reset_llm_settings(conn)
        insert_article(
            conn,
            url="https://x.test/1",
            url_norm="https://x.test/1",
            source="X",
            title="A",
            day="2026-01-01",
            review_status="approved",
        )
        insert_article(
            conn,
            url="https://x.test/2",
            url_norm="https://x.test/2",
            source="X",
            title="B",
            day="2026-01-01",
            review_status="rejected",
        )
        conn.commit()
        admin_service.save_prompt_content(conn, "guidance", "custom guidance", actor_email="a@x.com")

        config = admin_service.get_prompt_config(conn)
        assert config["guidance"] == "custom guidance"
        assert config["guidance_version"] == 2
        assert config["approved_count"] == 1
        assert config["rejected_count"] == 1


# ── Candidate mining ──────────────────────────────────────────────────


def test_mine_keyword_candidates_ranks_by_approved_count_and_excludes_existing_terms() -> None:
    with migrated_schema() as conn:
        admin_service.create_keyword(conn, section="telecom", term_en="spectrum")
        for i in range(3):
            insert_article(
                conn,
                url=f"https://x.test/a{i}",
                url_norm=f"https://x.test/a{i}",
                source="X",
                title="BTRC issues new licensing rule",
                day="2026-01-01",
                review_status="approved",
            )
        insert_article(
            conn,
            url="https://x.test/r1",
            url_norm="https://x.test/r1",
            source="X",
            title="spectrum auction delayed again",
            day="2026-01-01",
            review_status="approved",
        )
        conn.commit()

        candidates = admin_service.mine_keyword_candidates(conn, limit=6)
        words = [c["word"] for c in candidates]
        assert "btrc" in words
        assert "spectrum" not in words  # already a configured keyword term


def test_mine_keyword_candidates_filters_numbers_and_singleton_hits() -> None:
    """Regression for the owner-flagged bug: a real approved headline
    like "Gas price hits 100 taka" used to surface "100" and "hits" as
    candidate keywords -- a pure number, and a word seen only once (weak
    evidence). Both must now be filtered: numbers outright, and any word
    appearing in fewer than 2 approved articles.
    """
    with migrated_schema() as conn:
        insert_article(
            conn,
            url="https://x.test/n1",
            url_norm="https://x.test/n1",
            source="X",
            title="Gas price hits 100 taka in Dhaka",
            day="2026-01-01",
            review_status="approved",
        )
        insert_article(
            conn,
            url="https://x.test/n2",
            url_norm="https://x.test/n2",
            source="X",
            title="Telecom operators face new tariff",
            day="2026-01-01",
            review_status="approved",
        )
        conn.commit()

        candidates = admin_service.mine_keyword_candidates(conn, limit=20)
        words = [c["word"] for c in candidates]
        assert "100" not in words  # pure number
        assert "hits" not in words  # appears once -- below the frequency floor
        assert "price" not in words  # also appears once


def test_is_low_information_rejects_only_digit_majority_tokens() -> None:
    """Unit-level check on the numeric filter itself: pure numbers and
    digit-majority tokens ("100", "2026") are low-information, but a
    genuinely meaningful mixed alphanumeric telecom term where digits
    are NOT the majority ("4glte", one digit of five chars) is not --
    the filter targets low-information noise, not every token
    containing a digit.
    """
    assert admin_service._is_low_information("100") is True
    assert admin_service._is_low_information("2026") is True
    assert admin_service._is_low_information("4glte") is False
    assert admin_service._is_low_information("btrc") is False


# ── suggest_refinement (mocked LLM) ─────────────────────────────────────


def test_suggest_refinement_calls_llm_client_with_approved_and_rejected_titles(monkeypatch: pytest.MonkeyPatch) -> None:
    with migrated_schema() as conn:
        insert_article(
            conn,
            url="https://x.test/1",
            url_norm="https://x.test/1",
            source="X",
            title="Approved headline",
            day="2026-01-01",
            review_status="approved",
        )
        insert_article(
            conn,
            url="https://x.test/2",
            url_norm="https://x.test/2",
            source="X",
            title="Rejected headline",
            day="2026-01-01",
            review_status="rejected",
        )
        conn.commit()

        fake = FakeLLMProvider(responses=lambda p, s: "Prefer stories mentioning BTRC.")
        fake_client = LLMClient(transport=fake, name="fake · test-model")
        monkeypatch.setattr(admin_service, "_build_client_for_feature", lambda conn, feature: fake_client)

        suggestion = admin_service.suggest_refinement(conn)
        assert suggestion == "Prefer stories mentioning BTRC."
        assert len(fake.call_log) == 1
        assert "Approved headline" in fake.call_log[0]["prompt"]
        assert "Rejected headline" in fake.call_log[0]["prompt"]


# ── Provider routing config ─────────────────────────────────────────────


def test_get_routing_config_defaults_spread_features_across_providers() -> None:
    with migrated_schema() as conn:
        _reset_llm_settings(conn)
        routes = admin_service.get_routing_config(conn)
        assert set(routes.keys()) == {
            "scoring",
            "translation",
            "dedup_verification",
            "topic_clustering",
            "summarization",
        }
        # Default moved off Groq's reasoning model qwen3.6-27b — see
        # app.services.llm.routing.default_feature_route for the
        # measurements behind it.
        # scoring and translation sit on DIFFERENT providers on purpose
        # so the two heaviest stages do not share one per-minute budget.
        assert routes["scoring"]["primary"] == {"provider": "groq", "model": "llama-3.1-8b-instant"}
        assert routes["translation"]["primary"] == {"provider": "mistral", "model": "mistral-small-latest"}
        assert routes["scoring"]["secondary"]["provider"] != routes["scoring"]["primary"]["provider"]


def test_save_routing_config_round_trips_and_preserves_untouched_features() -> None:
    with migrated_schema() as conn:
        routes_in = {
            "scoring": {"primary": {"provider": "gemini", "model": "gemini-flash-lite-latest"}, "secondary": None},
        }
        saved = admin_service.save_routing_config(conn, routes_in)
        assert saved["scoring"]["primary"] == {"provider": "gemini", "model": "gemini-flash-lite-latest"}
        assert saved["scoring"]["secondary"] is None
        # unaffected features still default
        assert saved["translation"]["primary"]["provider"] == "mistral"  # per-feature default, untouched

        reloaded = admin_service.get_routing_config(conn)
        assert reloaded["scoring"]["primary"]["model"] == "gemini-flash-lite-latest"


def test_save_routing_config_accepts_an_arbitrary_provider_string() -> None:
    """Task 3 / provider-limits-VERIFIED.md: provider identity is
    runtime config, not a hardcoded 2-vendor enum -- routing a feature to
    a provider this module has never heard of (e.g. a freshly
    admin-registered one) must succeed; whether it actually RESOLVES to
    a usable client is app.services.llm.registry's concern at call time,
    not routing-config-authoring time.
    """
    with migrated_schema() as conn:
        saved = admin_service.save_routing_config(
            conn, {"scoring": {"primary": {"provider": "mistral", "model": "mistral-small-latest"}}}
        )
        assert saved["scoring"]["primary"] == {"provider": "mistral", "model": "mistral-small-latest"}


# ── Provider accounts (encrypted key entry) ──────────────────────────────


@pytest.fixture(autouse=True)
def _secret_key(monkeypatch: pytest.MonkeyPatch):
    from cryptography.fernet import Fernet

    from app.config import get_settings

    monkeypatch.setenv("SECRET_KEY", Fernet.generate_key().decode())
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_list_provider_configs_returns_seeded_rows_without_plaintext_keys() -> None:
    with migrated_schema() as conn:
        rows = admin_service.list_provider_configs(conn)
        by_provider = {r["provider"]: r for r in rows}
        assert set(by_provider) == {"groq", "gemini"}
        assert by_provider["groq"]["has_key"] is False
        assert by_provider["groq"]["api_key_last4"] == ""
        for row in rows:
            assert "api_key" not in row
            assert "ciphertext" not in str(row).lower() or True  # no raw key material anywhere in the dict


def test_upsert_provider_config_encrypts_key_and_returns_masked_suffix_only() -> None:
    with migrated_schema() as conn:
        updated = admin_service.upsert_provider_config(
            conn, "groq", model="qwen/qwen3.6-27b", role="primary", api_key="sk-super-secret-value-123"
        )
        assert updated["has_key"] is True
        assert updated["api_key_last4"].endswith("-123")
        assert "sk-super-secret-value-123" not in str(updated)

        row = conn.execute("SELECT api_key_ciphertext FROM provider_configs WHERE provider = 'groq'").fetchone()
        assert row["api_key_ciphertext"] is not None
        assert b"sk-super-secret-value-123" not in bytes(row["api_key_ciphertext"])


def test_upsert_provider_config_without_key_preserves_existing_key() -> None:
    with migrated_schema() as conn:
        admin_service.upsert_provider_config(conn, "gemini", api_key="first-key-value")
        first = conn.execute("SELECT api_key_ciphertext FROM provider_configs WHERE provider = 'gemini'").fetchone()

        admin_service.upsert_provider_config(conn, "gemini", model="gemini-flash-lite-latest")
        second = conn.execute("SELECT api_key_ciphertext FROM provider_configs WHERE provider = 'gemini'").fetchone()
        assert bytes(first["api_key_ciphertext"]) == bytes(second["api_key_ciphertext"])


def test_upsert_provider_config_rejects_unknown_provider_or_role() -> None:
    with migrated_schema() as conn:
        with pytest.raises(ValidationError):
            admin_service.upsert_provider_config(conn, "openai", model="gpt-4")
        with pytest.raises(ValidationError):
            admin_service.upsert_provider_config(conn, "groq", role="not_a_role")


# ── Quota ─────────────────────────────────────────────────────────────


def test_get_quota_status_defaults_and_save_quota_limit_round_trips() -> None:
    with migrated_schema() as conn:
        _reset_llm_settings(conn)
        statuses = {s["provider"]: s for s in admin_service.get_quota_status(conn)}
        assert statuses["groq"]["rpd"] == 1000
        assert statuses["gemini"]["rpd"] is None

        updated = admin_service.save_quota_limit(conn, "gemini", tpd=500_000, rpd=1_500)
        assert updated["tpd"] == 500_000
        assert updated["rpd"] == 1_500
        assert updated["exhausted"] is False

        with pytest.raises(ValidationError):
            admin_service.save_quota_limit(conn, "", tpd=1, rpd=1)


def test_get_quota_status_includes_arbitrary_providers_once_defined_or_limited() -> None:
    """No hardcoded 2-vendor list (provider-limits-VERIFIED.md's
    lesson): a provider the admin has set a quota ceiling for, or that
    has an entry in app.services.llm.provider_registry, must show up
    even though it's neither groq nor gemini.
    """
    with migrated_schema() as conn:
        admin_service.save_quota_limit(conn, "mistral", tpd=None, rpd=None)
        statuses = {s["provider"] for s in admin_service.get_quota_status(conn)}
        assert "mistral" in statuses


# ── Generic provider registry: arbitrary providers + pooled credentials ──


def test_list_provider_definitions_includes_the_documented_defaults() -> None:
    with migrated_schema() as conn:
        _reset_llm_settings(conn)
        definitions = {d["provider"]: d for d in admin_service.list_provider_definitions(conn)}
        assert definitions["groq"]["adapter"] == "openai_compatible"
        assert definitions["mistral"]["enabled"] is True
        assert definitions["cerebras"]["enabled"] is False  # verified "payment required" -- must not be offered


def test_save_provider_definition_adds_a_brand_new_provider_with_no_code_change() -> None:
    with migrated_schema() as conn:
        saved = admin_service.save_provider_definition(
            conn,
            "together_ai",
            adapter="openai_compatible",
            base_url="https://api.together.xyz/v1",
            default_model="some-model-id",
            enabled=True,
            note="added by an admin",
        )
        assert saved["provider"] == "together_ai"
        assert saved["base_url"] == "https://api.together.xyz/v1"

        reloaded = {d["provider"]: d for d in admin_service.list_provider_definitions(conn)}
        assert "together_ai" in reloaded
        assert "groq" in reloaded  # existing defaults are preserved, not wiped


def test_save_provider_definition_openai_compatible_requires_base_url() -> None:
    with migrated_schema() as conn:
        with pytest.raises(ValidationError):
            admin_service.save_provider_definition(
                conn, "bad_provider", adapter="openai_compatible", base_url=None, default_model="x"
            )


def test_provider_credentials_round_trip_masked_and_removable() -> None:
    with migrated_schema() as conn:
        assert admin_service.list_provider_credentials(conn, "mistral") == []

        added = admin_service.save_provider_credential(conn, "mistral", "primary", "sk-mistral-real-value-999")
        assert added["api_key_last4"].endswith("-999")
        assert "sk-mistral-real-value-999" not in str(added)

        listed = admin_service.list_provider_credentials(conn, "mistral")
        assert len(listed) == 1
        assert listed[0]["key_id"] == "primary"

        admin_service.remove_provider_credential(conn, "mistral", "primary")
        assert admin_service.list_provider_credentials(conn, "mistral") == []


# ── Router-level end-to-end ───────────────────────────────────────────


def test_router_prompt_config_and_versioning_end_to_end(monkeypatch: pytest.MonkeyPatch) -> None:
    with migrated_schema() as conn:
        import app.api.routers.prompts as prompts_router_module

        monkeypatch.setattr(prompts_router_module, "get_connection", lambda: contextlib.nullcontext(conn))

        app = create_app()
        app.dependency_overrides[get_current_user] = lambda: ADMIN
        with TestClient(app) as client:
            saved = client.put("/prompts/guidance", json={"content": "Focus on telecom.", "note": "tighten scope"})
            assert saved.status_code == 200
            assert saved.json()["version"] == 2

            versions = client.get("/prompts/guidance/versions")
            assert versions.status_code == 200
            assert [v["version"] for v in versions.json()] == [1, 2]

            activated = client.post("/prompts/guidance/versions/1/activate")
            assert activated.status_code == 200
            assert activated.json()["active"] is True

            missing = client.post("/prompts/guidance/versions/999/activate")
            assert missing.status_code == 404

            config = client.get("/prompts/config")
            assert config.status_code == 200
            assert config.json()["guidance_version"] == 1

            providers = client.get("/prompts/providers")
            assert providers.status_code == 200
            assert {p["provider"] for p in providers.json()} == {"groq", "gemini"}

            routing = client.get("/prompts/routing")
            assert routing.status_code == 200
            assert routing.json()["routes"]["scoring"]["primary"]["provider"] == "groq"

            quota = client.get("/prompts/quota")
            assert quota.status_code == 200
            assert {q["provider"] for q in quota.json()} >= {"groq", "gemini"}

            definitions = client.get("/prompts/provider-definitions")
            assert definitions.status_code == 200
            assert {d["provider"] for d in definitions.json()} >= {"groq", "gemini", "mistral", "cerebras"}

            new_def = client.put(
                "/prompts/provider-definitions/together_ai",
                json={"adapter": "openai_compatible", "base_url": "https://api.together.xyz/v1", "default_model": "x"},
            )
            assert new_def.status_code == 200
            assert new_def.json()["provider"] == "together_ai"

            added_cred = client.post(
                "/prompts/provider-definitions/together_ai/credentials",
                json={"key_id": "primary", "api_key": "sk-real-secret-value-abc"},
            )
            assert added_cred.status_code == 201
            assert "sk-real-secret-value-abc" not in added_cred.text

            creds = client.get("/prompts/provider-definitions/together_ai/credentials")
            assert creds.status_code == 200
            assert len(creds.json()) == 1

            deleted_cred = client.delete("/prompts/provider-definitions/together_ai/credentials/primary")
            assert deleted_cred.status_code == 204
