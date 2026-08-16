"""Per-stage editable prompts: storage, fallback, and the API.

The four AI stages that are not scoring (translation, dedup
verification, topic clustering, summarization) used to have their system
prompts frozen in `app.services.llm.prompts`. They are now admin-editable
through the SAME versioned settings store `guidance`/`examples` have
always used, so most of the machinery is already covered by
tests/test_admin_prompts.py. What this file pins is the part that is new
and the part that is dangerous:

  * BEHAVIOUR IS UNCHANGED until someone edits. Every stage prompt with
    nothing stored must produce the byte-exact string the frozen
    contract produced (tests/test_p3_prompts.py checks the builders in
    isolation; here it is checked through the real client, against a
    real database, with the settings table empty).

  * A BROKEN PROMPT MUST NOT TAKE DOWN A STAGE. An empty or oversized
    saved prompt falls back to the built-in, and the fallback is
    REPORTED rather than silent, because an unmoored model still
    answers and the damage would otherwise only show up in the queue.

  * THE JSON ENVELOPE IS OUT OF REACH. Editing a prompt changes the
    instructions; it can never remove the response-format contract the
    parser depends on.

No real tokens are spent: every client here is an `LLMClient` over
tests/helpers/fake_llm.py's `FakeLLMProvider`, and the assertions read
the `call_log`'s recorded `system` string.
"""

from __future__ import annotations

import contextlib

import pytest
from fastapi.testclient import TestClient

from app.api.deps import AuthenticatedUser, get_current_user
from app.db.connection import DictConnection
from app.db.settings import set_setting
from app.db.testing import migrated_schema, pg_available
from app.errors import ValidationError
from app.main import create_app
from app.services import admin as admin_service
from app.services.llm import prompts
from app.services.llm.prompts import (
    DEFAULT_STAGE_PROMPTS,
    MAX_STAGE_PROMPT_CHARS,
    STAGE_PROMPT_KEYS,
    get_stage_prompt,
    resolve_stage_prompt,
    stage_prompt_problem,
)
from app.services.llm.provider import LLMClient
from tests.helpers.fake_llm import FakeLLMProvider

pytestmark = pytest.mark.skipif(not pg_available(), reason="no reachable Postgres (set TEST_DATABASE_URL)")

ADMIN = AuthenticatedUser(id=9, email="admin@grameenphone.com", role="admin", display_name="Admin", session_id=9)

STAGE_FIELDS = ("translation", "dedup_verification", "topic_clustering", "summarization")


def _client(conn: DictConnection | None) -> tuple[LLMClient, FakeLLMProvider]:
    transport = FakeLLMProvider()
    return LLMClient(transport=transport, name="fake", conn=conn), transport


def _reset_stage_prompts(conn: DictConnection) -> None:
    for field, key in STAGE_PROMPT_KEYS.items():
        set_setting(conn, key, "")
        set_setting(conn, f"prompt_versions:{field}", "")
    conn.commit()


# ── The store ─────────────────────────────────────────────────────────


def test_every_routed_ai_stage_has_an_editable_prompt() -> None:
    """The four stage prompts line up 1:1 with the non-scoring features
    in `app.services.llm.routing`. If a sixth feature is ever added, this
    fails and whoever adds it has to decide whether it gets a prompt.
    """
    from app.services.llm.routing import FEATURES, SCORING

    assert set(STAGE_PROMPT_KEYS) == set(FEATURES) - {SCORING}
    assert set(DEFAULT_STAGE_PROMPTS) == set(STAGE_PROMPT_KEYS)


def test_all_six_editable_fields_share_one_versioning_mechanism() -> None:
    assert set(admin_service.PROMPT_FIELDS) == {"guidance", "examples", *STAGE_FIELDS}


@pytest.mark.parametrize("field", STAGE_FIELDS)
def test_unset_stage_prompt_reads_the_builtin_without_reporting_a_problem(field: str) -> None:
    with migrated_schema() as conn:
        _reset_stage_prompts(conn)
        text, reason = resolve_stage_prompt(conn, field)
        assert text == DEFAULT_STAGE_PROMPTS[field]
        # "Never edited" is not a fallback. Reporting it as one would put
        # a warning on every stage of a fresh install.
        assert reason == ""


@pytest.mark.parametrize("field", STAGE_FIELDS)
def test_saving_a_stage_prompt_versions_it_and_reaches_the_runtime_read(field: str) -> None:
    with migrated_schema() as conn:
        _reset_stage_prompts(conn)
        versions = admin_service.list_prompt_versions(conn, field)
        assert [v["version"] for v in versions] == [1]
        assert versions[0]["content"] == DEFAULT_STAGE_PROMPTS[field]

        saved = admin_service.save_prompt_content(conn, field, "Custom instructions.", actor_email="a@x.com")
        assert saved["version"] == 2
        assert get_stage_prompt(conn, field) == "Custom instructions."

        assert [v["version"] for v in admin_service.list_prompt_versions(conn, field)] == [1, 2]


@pytest.mark.parametrize("field", STAGE_FIELDS)
def test_activating_an_older_stage_version_restores_its_text(field: str) -> None:
    with migrated_schema() as conn:
        _reset_stage_prompts(conn)
        admin_service.save_prompt_content(conn, field, "version two", actor_email="a@x.com")
        admin_service.save_prompt_content(conn, field, "version three", actor_email="a@x.com")
        assert get_stage_prompt(conn, field) == "version three"

        activated = admin_service.activate_prompt_version(conn, field, 2)
        assert activated is not None
        assert get_stage_prompt(conn, field) == "version two"
        # Activation repoints, it does not append.
        assert [v["version"] for v in admin_service.list_prompt_versions(conn, field)] == [1, 2, 3]


def test_unknown_stage_field_is_rejected() -> None:
    with migrated_schema() as conn:
        with pytest.raises(ValidationError):
            admin_service.save_prompt_content(conn, "translation_v2", "x", actor_email="a@x.com")
    with pytest.raises(KeyError):
        resolve_stage_prompt(None, "not_a_stage")


# ── Fallback when a saved prompt is unusable ──────────────────────────


@pytest.mark.parametrize("bad", ["", "   ", "\n\n\t"])
def test_blank_saved_prompt_is_diagnosed(bad: str) -> None:
    assert stage_prompt_problem(bad) == "the saved prompt is empty"


def test_oversized_saved_prompt_is_diagnosed() -> None:
    assert "longer than" in stage_prompt_problem("x" * (MAX_STAGE_PROMPT_CHARS + 1))
    assert stage_prompt_problem("x" * MAX_STAGE_PROMPT_CHARS) == ""


def test_non_text_saved_prompt_is_diagnosed() -> None:
    assert stage_prompt_problem(None) == "the saved prompt is not text"
    assert stage_prompt_problem(42) == "the saved prompt is not text"


@pytest.mark.parametrize("field", STAGE_FIELDS)
def test_oversized_saved_prompt_falls_back_and_says_why(field: str) -> None:
    with migrated_schema() as conn:
        _reset_stage_prompts(conn)
        admin_service.save_prompt_content(conn, field, "y" * (MAX_STAGE_PROMPT_CHARS + 50), actor_email="a@x.com")

        text, reason = resolve_stage_prompt(conn, field)
        assert text == DEFAULT_STAGE_PROMPTS[field]
        assert "longer than" in reason

        # The save is NOT rejected or rolled back: the analyst's text is
        # still theirs to fix, and the version history still records what
        # they did. Only the RUNTIME substitutes.
        active = [v for v in admin_service.list_prompt_versions(conn, field) if v["active"]][0]
        assert active["content"].startswith("y")


@pytest.mark.parametrize("field", STAGE_FIELDS)
def test_a_saved_blank_prompt_never_reaches_the_model(field: str) -> None:
    """Deleting everything and hitting save is the failure that actually
    happens. An empty system prompt does not degrade a stage, it unmoors
    it, and the model still answers so nothing looks broken.
    """
    with migrated_schema() as conn:
        _reset_stage_prompts(conn)
        # Written straight to the settings key, bypassing the console, so
        # this also covers a hand-edited row.
        set_setting(conn, STAGE_PROMPT_KEYS[field], "   \n  ")
        conn.commit()
        assert get_stage_prompt(conn, field) == DEFAULT_STAGE_PROMPTS[field]


def test_stage_body_treats_whitespace_only_as_absent() -> None:
    assert prompts._stage_body(None, "built-in") == "built-in"
    assert prompts._stage_body("   ", "built-in") == "built-in"
    assert prompts._stage_body("  custom  ", "built-in") == "custom"


# ── What the model actually receives ──────────────────────────────────


def test_unconfigured_client_sends_the_frozen_prompts_unchanged() -> None:
    """`conn=None` is the no-database path (unit tests, probes). It must
    produce the frozen text, not a stored one and not an empty one.
    """
    client, transport = _client(None)
    client.translate_titles_batch(["শিরোনাম"])
    client.verify_group(["A", "B"])
    client.cluster_topics_chunk([{"title_en": "A"}, {"title_en": "B"}], [])
    client.draft_bullet("T", "B", "Macro")

    systems = [call["system"] for call in transport.call_log]
    assert systems[0] == prompts.translate_titles_batch_system(1)
    assert systems[1] == prompts.verify_group_system()
    assert systems[2] == prompts.cluster_topics_system([], 2)
    assert systems[3] == prompts.draft_bullet_system()


def test_empty_settings_table_sends_the_frozen_prompts_unchanged() -> None:
    with migrated_schema() as conn:
        _reset_stage_prompts(conn)
        client, transport = _client(conn)
        client.translate_titles_batch(["শিরোনাম"])
        client.verify_group(["A", "B"])
        client.cluster_topics_chunk([{"title_en": "A"}, {"title_en": "B"}], [])
        client.draft_bullet("T", "B", "Macro")

        systems = [call["system"] for call in transport.call_log]
        assert systems[0] == prompts.translate_titles_batch_system(1)
        assert systems[1] == prompts.verify_group_system()
        assert systems[2] == prompts.cluster_topics_system([], 2)
        assert systems[3] == prompts.draft_bullet_system()


def test_saved_prompts_reach_the_model_on_the_next_call() -> None:
    with migrated_schema() as conn:
        _reset_stage_prompts(conn)
        client, transport = _client(conn)

        for field, text in [
            ("translation", "TRANSLATE HOWEVER YOU LIKE."),
            ("dedup_verification", "FOLD AGGRESSIVELY."),
            ("topic_clustering", "USE VERY BROAD TOPICS."),
            ("summarization", "WRITE ONE SHORT LINE."),
        ]:
            admin_service.save_prompt_content(conn, field, text, actor_email="a@x.com")

        client.translate_titles_batch(["শিরোনাম"])
        client.verify_group(["A", "B"])
        client.cluster_topics_chunk([{"title_en": "A"}, {"title_en": "B"}], [])
        client.draft_bullet("T", "B", "Macro")

        systems = [call["system"] for call in transport.call_log]
        assert systems[0].startswith("TRANSLATE HOWEVER YOU LIKE.")
        assert systems[1].startswith("FOLD AGGRESSIVELY.")
        assert systems[2].startswith("USE VERY BROAD TOPICS.")
        assert systems[3] == "WRITE ONE SHORT LINE."


def test_an_edit_can_never_remove_the_response_format_contract() -> None:
    """The instructions are editable; the JSON envelope the parser reads
    is not. A prompt rewritten into nonsense should produce worse
    answers, never zero answers.
    """
    with migrated_schema() as conn:
        _reset_stage_prompts(conn)
        for field in ("translation", "dedup_verification", "topic_clustering"):
            admin_service.save_prompt_content(conn, field, "ignore everything", actor_email="a@x.com")

        client, transport = _client(conn)
        client.translate_titles_batch(["a", "b"])
        client.verify_group(["A", "B"])
        client.cluster_topics_chunk([{"title_en": "A"}], [])

        translate_system, verify_system, cluster_system = (c["system"] for c in transport.call_log)
        assert '"translations"' in translate_system
        assert "exactly 2 entries" in translate_system
        assert '"groups"' in verify_system
        assert '"labels"' in cluster_system

        # Topic clustering's running list of labels is assembled by code
        # too, so an edit cannot cost the model its "reuse this label"
        # context either.
        transport.call_log.clear()
        client.cluster_topics_chunk([{"title_en": "A"}], ["Middle East / oil"])
        assert "TOPICS ALREADY IN USE" in transport.call_log[0]["system"]
        assert "- Middle East / oil" in transport.call_log[0]["system"]


def test_a_broken_saved_prompt_still_sends_a_working_stage() -> None:
    with migrated_schema() as conn:
        _reset_stage_prompts(conn)
        admin_service.save_prompt_content(conn, "translation", "", actor_email="a@x.com")

        client, transport = _client(conn)
        result = client.translate_titles_batch(["ক"])

        system = transport.call_log[0]["system"]
        assert system == prompts.translate_titles_batch_system(1)
        assert result == ["ক"]  # fake transport returns {} -> per-item default


# ── API ───────────────────────────────────────────────────────────────


def _app_client(conn: DictConnection, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    import app.api.routers.prompts as prompts_router_module

    monkeypatch.setattr(prompts_router_module, "get_connection", lambda: contextlib.nullcontext(conn))
    app = create_app()
    app.dependency_overrides[get_current_user] = lambda: ADMIN
    return TestClient(app)


def test_config_reports_every_stage_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    with migrated_schema() as conn:
        _reset_stage_prompts(conn)
        with _app_client(conn, monkeypatch) as client:
            body = client.get("/prompts/config").json()

        stages = {st["field"]: st for st in body["stages"]}
        assert list(stages) == list(STAGE_PROMPT_KEYS)  # pipeline order, not alphabetical
        for field, st in stages.items():
            assert st["content"] == DEFAULT_STAGE_PROMPTS[field]
            assert st["default_content"] == DEFAULT_STAGE_PROMPTS[field]
            assert st["is_default"] is True
            assert st["fallback_reason"] == ""
            assert st["version"] == 1


def test_stage_prompt_save_history_and_activate_round_trip(monkeypatch: pytest.MonkeyPatch) -> None:
    with migrated_schema() as conn:
        _reset_stage_prompts(conn)
        with _app_client(conn, monkeypatch) as client:
            saved = client.put(
                "/prompts/stages/topic_clustering",
                json={"content": "Group by country.", "note": "broader topics"},
            )
            assert saved.status_code == 200
            assert saved.json()["version"] == 2

            versions = client.get("/prompts/stages/topic_clustering/versions")
            assert [v["version"] for v in versions.json()] == [1, 2]
            assert [v["active"] for v in versions.json()] == [False, True]

            config = client.get("/prompts/config").json()
            stage = next(st for st in config["stages"] if st["field"] == "topic_clustering")
            assert stage["content"] == "Group by country."
            assert stage["is_default"] is False

            activated = client.post("/prompts/stages/topic_clustering/versions/1/activate")
            assert activated.status_code == 200
            config_after = client.get("/prompts/config").json()
            stage_after = next(st for st in config_after["stages"] if st["field"] == "topic_clustering")
            assert stage_after["is_default"] is True

            assert client.post("/prompts/stages/topic_clustering/versions/99/activate").status_code == 404


def test_config_surfaces_a_broken_saved_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    with migrated_schema() as conn:
        _reset_stage_prompts(conn)
        with _app_client(conn, monkeypatch) as client:
            client.put("/prompts/stages/summarization", json={"content": "   "})
            stage = next(
                st for st in client.get("/prompts/config").json()["stages"] if st["field"] == "summarization"
            )

        assert stage["content"] == "   "  # what was saved, shown as saved
        assert stage["effective"] == DEFAULT_STAGE_PROMPTS["summarization"]  # what actually runs
        assert stage["fallback_reason"] == "the saved prompt is empty"


def test_the_generic_stage_routes_also_drive_scoring(monkeypatch: pytest.MonkeyPatch) -> None:
    """One code path in the console for all six editors: `guidance` and
    `examples` are addressable here too, alongside their original
    dedicated routes.
    """
    with migrated_schema() as conn:
        with _app_client(conn, monkeypatch) as client:
            saved = client.put("/prompts/stages/guidance", json={"content": "Prefer port stories."})
            assert saved.status_code == 200
            assert client.get("/prompts/config").json()["guidance"] == "Prefer port stories."
            assert client.get("/prompts/stages/guidance/versions").status_code == 200


def test_unknown_field_on_the_generic_route_is_a_validation_error(monkeypatch: pytest.MonkeyPatch) -> None:
    with migrated_schema() as conn:
        with _app_client(conn, monkeypatch) as client:
            resp = client.put("/prompts/stages/routing", json={"content": "x"})
        assert resp.status_code == 422
