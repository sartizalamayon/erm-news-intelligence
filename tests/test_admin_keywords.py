"""Keyword CRUD + rematch (app.services.admin, app.api.routers.keywords).
Service-layer tests against a real, migrated Postgres schema
(app.db.testing.migrated_schema); a couple of router-level tests for the
end-to-end HTTP wiring.
"""

from __future__ import annotations

import contextlib
import datetime as dt

import pytest
from fastapi.testclient import TestClient

from app.api.deps import AuthenticatedUser, get_current_user
from app.db.queries import insert_article
from app.db.testing import migrated_schema, pg_available
from app.errors import ValidationError
from app.main import create_app
from app.services import admin as admin_service

pytestmark = pytest.mark.skipif(not pg_available(), reason="no reachable Postgres (set TEST_DATABASE_URL)")

ADMIN = AuthenticatedUser(id=9, email="admin@grameenphone.com", role="admin", display_name="Admin", session_id=9)


def test_create_list_get_keyword() -> None:
    with migrated_schema() as conn:
        created = admin_service.create_keyword(conn, section="telecom", term_en="spectrum", term_bn="স্পেকট্রাম")
        assert created["section"] == "telecom"
        assert created["enabled"] is True

        fetched = admin_service.get_keyword(conn, created["id"])
        assert fetched == created

        listed = admin_service.list_keywords(conn, section="telecom")
        assert [k["id"] for k in listed] == [created["id"]]


def test_create_keyword_requires_a_term() -> None:
    with migrated_schema() as conn:
        with pytest.raises(ValidationError):
            admin_service.create_keyword(conn, section="telecom", term_en="", term_bn="")


def test_create_keyword_english_only_term_is_valid() -> None:
    with migrated_schema() as conn:
        created = admin_service.create_keyword(conn, section="telecom", term_en="SMP", term_bn="")
        assert created["term_bn"] == ""


def test_list_keyword_sections_counts_per_section() -> None:
    with migrated_schema() as conn:
        admin_service.create_keyword(conn, section="telecom", term_en="spectrum")
        admin_service.create_keyword(conn, section="telecom", term_en="BTRC")
        admin_service.create_keyword(conn, section="macro", term_en="reserves")
        sections = {s["section"]: s["count"] for s in admin_service.list_keyword_sections(conn)}
        assert sections == {"telecom": 2, "macro": 1}


def test_update_keyword_partial_and_toggle() -> None:
    with migrated_schema() as conn:
        created = admin_service.create_keyword(conn, section="telecom", term_en="5G")
        updated = admin_service.update_keyword(conn, created["id"], term_bn="৫জি")
        assert updated["term_bn"] == "৫জি"
        assert updated["term_en"] == "5G"

        toggled = admin_service.toggle_keyword(conn, created["id"])
        assert toggled["enabled"] is False


def test_update_delete_unknown_keyword_returns_none_or_false() -> None:
    with migrated_schema() as conn:
        assert admin_service.update_keyword(conn, 999999, enabled=False) is None
        assert admin_service.delete_keyword(conn, 999999) is False


def test_delete_keyword_removes_row() -> None:
    with migrated_schema() as conn:
        created = admin_service.create_keyword(conn, section="telecom", term_en="NEIR")
        assert admin_service.delete_keyword(conn, created["id"]) is True
        assert admin_service.get_keyword(conn, created["id"]) is None


# ── rematch_today ──────────────────────────────────────────────────────


def _today_article(conn, **overrides) -> int:
    fields = {
        "url": overrides.pop("url", "https://example.com/a"),
        "url_norm": overrides.pop("url_norm", "https://example.com/a"),
        "source": "Example",
        "title": overrides.pop("title", "BTRC fines Summit for bandwidth pricing"),
        "day": dt.date.today(),
        "review_status": overrides.pop("review_status", "pending"),
    }
    fields.update(overrides)
    article_id = insert_article(conn, **fields)
    assert article_id is not None
    return article_id


def test_rematch_today_matches_and_filters_pending_articles() -> None:
    with migrated_schema() as conn:
        admin_service.create_keyword(conn, section="telecom", term_en="BTRC")
        matching_id = _today_article(
            conn, url="https://x.test/1", url_norm="https://x.test/1", title="BTRC issues new rule"
        )
        non_matching_id = _today_article(
            conn, url="https://x.test/2", url_norm="https://x.test/2", title="Completely unrelated sports story"
        )
        conn.commit()

        result = admin_service.rematch_today(conn)
        assert result == {"matched": 1, "filtered_out": 1, "total": 2}

        matched_row = conn.execute(
            "SELECT review_status, keywords FROM articles WHERE id = %s", (matching_id,)
        ).fetchone()
        assert matched_row["review_status"] == "pending"
        assert "BTRC" in matched_row["keywords"]

        filtered_row = conn.execute("SELECT review_status FROM articles WHERE id = %s", (non_matching_id,)).fetchone()
        assert filtered_row["review_status"] == "filtered_out"


def test_rematch_today_never_touches_already_decided_articles() -> None:
    with migrated_schema() as conn:
        # No "BTRC" keyword configured at all -- if rematch touched this
        # approved row it would flip to filtered_out, which must not happen.
        approved_id = _today_article(
            conn, url="https://x.test/approved", url_norm="https://x.test/approved", review_status="approved"
        )
        conn.commit()

        result = admin_service.rematch_today(conn)
        assert result["total"] == 0

        row = conn.execute("SELECT review_status FROM articles WHERE id = %s", (approved_id,)).fetchone()
        assert row["review_status"] == "approved"


# ── Router-level ──────────────────────────────────────────────────────


def test_router_full_crud_and_rematch_flow_end_to_end(monkeypatch: pytest.MonkeyPatch) -> None:
    with migrated_schema() as conn:
        import app.api.routers.keywords as keywords_router_module

        monkeypatch.setattr(keywords_router_module, "get_connection", lambda: contextlib.nullcontext(conn))

        app = create_app()
        app.dependency_overrides[get_current_user] = lambda: ADMIN
        with TestClient(app) as client:
            created = client.post("/keywords", json={"section": "telecom", "term_en": "spectrum"})
            assert created.status_code == 201
            keyword_id = created.json()["id"]

            sections = client.get("/keywords/sections")
            assert sections.status_code == 200
            assert sections.json() == [{"section": "telecom", "count": 1}]

            toggled = client.post(f"/keywords/{keyword_id}/toggle")
            assert toggled.status_code == 200
            assert toggled.json()["enabled"] is False

            rematch = client.post("/keywords/rematch")
            assert rematch.status_code == 200
            assert rematch.json() == {"matched": 0, "filtered_out": 0, "total": 0}

            deleted = client.delete(f"/keywords/{keyword_id}")
            assert deleted.status_code == 204

            missing = client.patch(f"/keywords/{keyword_id}", json={"enabled": True})
            assert missing.status_code == 404
