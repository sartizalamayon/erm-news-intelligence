"""Tests for the Review queue (frontend-contract.md §3.3): GET/PATCH
/articles, POST /articles/bulk, POST /articles/{id}/dups/{dup_id}/keep|drop.
"""

from __future__ import annotations

import contextlib
import itertools
from datetime import date

import pytest
from fastapi.testclient import TestClient

from app.api.deps import AuthenticatedUser, get_current_user
from app.db.testing import migrated_schema, pg_available
from app.main import create_app

pytestmark = pytest.mark.skipif(not pg_available(), reason="no reachable Postgres (set TEST_DATABASE_URL)")

USER = AuthenticatedUser(id=1, email="analyst@grameenphone.com", role="analyst", display_name="Analyst", session_id=1)

_url_seq = itertools.count()


def _insert_article(conn, **fields):
    defaults = {
        "url": f"https://x.test/article-{next(_url_seq)}",
        "url_norm": "",
        "source": "Prothom Alo",
        "title": "Test headline",
        "day": date.today(),
        "sections": "",
        "keywords": "",
        "review_status": "pending",
    }
    defaults.update(fields)
    columns = list(defaults.keys())
    placeholders = ", ".join(f"%({c})s" for c in columns)
    row = conn.execute(
        f"INSERT INTO articles ({', '.join(columns)}) VALUES ({placeholders}) RETURNING id", defaults
    ).fetchone()
    return row["id"]


@pytest.fixture
def review_client(monkeypatch: pytest.MonkeyPatch):
    import app.api.routers.review as review_router_module

    with migrated_schema() as conn:
        monkeypatch.setattr(review_router_module, "get_connection", lambda: contextlib.nullcontext(conn))
        app = create_app()
        app.dependency_overrides[get_current_user] = lambda: USER
        with TestClient(app) as client:
            yield client, conn


def test_list_articles_unauthenticated_returns_401() -> None:
    app = create_app()
    with TestClient(app) as client:
        resp = client.get("/articles")
    assert resp.status_code == 401


def test_patch_article_unauthenticated_returns_401() -> None:
    app = create_app()
    with TestClient(app) as client:
        resp = client.patch("/articles/1", json={"status": "approved"})
    assert resp.status_code == 401


def test_list_excludes_filtered_out_and_includes_pending(review_client) -> None:
    client, conn = review_client
    _insert_article(conn, title="Matched one", sections="Macroeconomy", keywords="inflation", llm_score=8, review_status="pending")
    _insert_article(conn, title="Unmatched", sections="", keywords="", review_status="filtered_out")
    conn.commit()

    resp = client.get("/articles")
    assert resp.status_code == 200
    data = resp.json()
    titles = [a["title"] for a in data["items"]]
    assert "Matched one" in titles
    assert "Unmatched" not in titles
    item = next(a for a in data["items"] if a["title"] == "Matched one")
    assert item["status"] == "pending"
    assert item["score"] == 8
    assert item["section"] == "Macroeconomy"
    assert item["keywords"] == ["inflation"]
    assert item["dups"] is None
    assert item["topicPeers"] is None


def test_list_status_filter_shows_only_pending_by_default_when_status_given(review_client) -> None:
    client, conn = review_client
    _insert_article(conn, title="Pending one", sections="Others", review_status="pending")
    _insert_article(conn, title="Approved one", sections="Others", review_status="approved")
    conn.commit()

    resp = client.get("/articles", params={"status": "pending"})
    assert resp.status_code == 200
    titles = [a["title"] for a in resp.json()["items"]]
    assert titles == ["Pending one"]

    resp_all = client.get("/articles")
    titles_all = {a["title"] for a in resp_all.json()["items"]}
    assert titles_all == {"Pending one", "Approved one"}


def test_list_tier_filter_bands_by_score(review_client) -> None:
    client, conn = review_client
    _insert_article(conn, title="High", sections="Others", llm_score=9)
    _insert_article(conn, title="Least", sections="Others", llm_score=2)
    conn.commit()

    resp = client.get("/articles", params={"tier": "red"})
    titles = [a["title"] for a in resp.json()["items"]]
    assert titles == ["High"]


def test_list_lang_filter_uses_bengali_unicode_detection(review_client) -> None:
    client, conn = review_client
    _insert_article(conn, title="BTRC moves to reprice spectrum", sections="Others")
    _insert_article(conn, title="বিটিআরসি নতুন নিয়ম করেছে", sections="Others")
    conn.commit()

    resp_en = client.get("/articles", params={"lang": "en"})
    assert [a["title"] for a in resp_en.json()["items"]] == ["BTRC moves to reprice spectrum"]

    resp_bn = client.get("/articles", params={"lang": "bn"})
    assert [a["title"] for a in resp_bn.json()["items"]] == ["বিটিআরসি নতুন নিয়ম করেছে"]


def test_list_sources_menu_reflects_counts(review_client) -> None:
    client, conn = review_client
    _insert_article(conn, title="A", source="Daily Star", sections="Others")
    _insert_article(conn, title="B", source="Daily Star", sections="Others")
    _insert_article(conn, title="C", source="Prothom Alo", sections="Others")
    conn.commit()

    resp = client.get("/articles")
    sources = {s["name"]: s["count"] for s in resp.json()["sources"]}
    assert sources == {"Daily Star": 2, "Prothom Alo": 1}


def test_patch_unknown_article_returns_404(review_client) -> None:
    client, _conn = review_client
    resp = client.patch("/articles/999999", json={"status": "approved"})
    assert resp.status_code == 404


def test_patch_invalid_status_returns_422(review_client) -> None:
    client, conn = review_client
    aid = _insert_article(conn, title="X", sections="Others")
    conn.commit()
    resp = client.patch(f"/articles/{aid}", json={"status": "filtered_out"})
    assert resp.status_code == 422


def test_patch_approve_sets_status_and_reviewer(review_client) -> None:
    client, conn = review_client
    aid = _insert_article(conn, title="X", sections="Others")
    conn.commit()

    resp = client.patch(f"/articles/{aid}", json={"status": "approved"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "approved"
    row = conn.execute("SELECT reviewed_by, reviewed_at FROM articles WHERE id = %s", (aid,)).fetchone()
    assert row["reviewed_by"] == USER.email
    assert row["reviewed_at"] is not None


def test_patch_star_and_note_persist_via_settings(review_client) -> None:
    client, conn = review_client
    aid = _insert_article(conn, title="X", sections="Others")
    conn.commit()

    resp = client.patch(f"/articles/{aid}", json={"starred": True, "note": "watch this one"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["isStarred"] is True
    assert body["note"] == "watch this one"

    # Round-trips on a fresh GET too, not just the PATCH response.
    listed = client.get("/articles").json()["items"]
    item = next(a for a in listed if a["id"] == aid)
    assert item["isStarred"] is True
    assert item["note"] == "watch this one"

    # Clearing the note deletes the settings row rather than leaving "".
    client.patch(f"/articles/{aid}", json={"note": ""})
    row = conn.execute("SELECT 1 FROM settings WHERE key = %s", (f"article_note::{aid}",)).fetchone()
    assert row is None


def test_bulk_approve_updates_all_ids(review_client) -> None:
    client, conn = review_client
    a1 = _insert_article(conn, title="A", sections="Others")
    a2 = _insert_article(conn, title="B", sections="Others")
    conn.commit()

    resp = client.post("/articles/bulk", json={"ids": [a1, a2], "action": "approve"})
    assert resp.status_code == 200
    assert resp.json()["updated"] == 2
    statuses = {
        row["id"]: row["review_status"]
        for row in conn.execute("SELECT id, review_status FROM articles WHERE id = ANY(%s)", ([a1, a2],)).fetchall()
    }
    assert statuses == {a1: "approved", a2: "approved"}


def test_bulk_star_sets_starred_flag(review_client) -> None:
    client, conn = review_client
    aid = _insert_article(conn, title="A", sections="Others")
    conn.commit()

    resp = client.post("/articles/bulk", json={"ids": [aid], "action": "star"})
    assert resp.status_code == 200
    item = next(a for a in client.get("/articles").json()["items"] if a["id"] == aid)
    assert item["isStarred"] is True


def test_dup_keep_promotes_the_chosen_sibling_to_primary(review_client) -> None:
    client, conn = review_client
    group = 4242
    primary = _insert_article(conn, title="Original", source="Daily Star", sections="Others", dup_group=group, is_primary=True)
    sibling = _insert_article(conn, title="Duplicate", source="The Business Standard", sections="Others", dup_group=group, is_primary=False)
    conn.commit()

    resp = client.post(f"/articles/{primary}/dups/{sibling}/keep")
    assert resp.status_code == 200
    assert resp.json()["id"] == sibling

    rows = {
        r["id"]: (r["is_primary"], r["also_in"])
        for r in conn.execute("SELECT id, is_primary, also_in FROM articles WHERE dup_group = %s", (group,)).fetchall()
    }
    assert rows[sibling][0] is True
    assert rows[sibling][1] == "Daily Star"
    assert rows[primary][0] is False
    assert rows[primary][1] == ""


def test_dup_drop_removes_sibling_from_group(review_client) -> None:
    client, conn = review_client
    group = 4343
    primary = _insert_article(conn, title="Original", source="Daily Star", sections="Others", dup_group=group, is_primary=True)
    sibling = _insert_article(conn, title="Duplicate", source="The Business Standard", sections="Others", dup_group=group, is_primary=False)
    conn.commit()

    resp = client.post(f"/articles/{primary}/dups/{sibling}/drop")
    assert resp.status_code == 200

    dropped = conn.execute("SELECT dup_group, is_primary, also_in FROM articles WHERE id = %s", (sibling,)).fetchone()
    assert dropped["dup_group"] is None
    assert dropped["is_primary"] is True
    assert dropped["also_in"] == ""
    # Sole survivor of a 2-member group is ungrouped too (see review.py's drop_dup).
    remaining = conn.execute("SELECT dup_group FROM articles WHERE id = %s", (primary,)).fetchone()
    assert remaining["dup_group"] is None


def test_dup_keep_unknown_dup_id_returns_404(review_client) -> None:
    client, conn = review_client
    primary = _insert_article(conn, title="Original", sections="Others", dup_group=99, is_primary=True)
    conn.commit()
    resp = client.post(f"/articles/{primary}/dups/999999/keep")
    assert resp.status_code == 404
