"""Tests for the Search archive (frontend-contract.md §3.6): GET /search.
Exercises the real `idx_articles_search_vec` GIN index against a
migrated Postgres schema — no network involved (full-text search is a
plain SQL query, not an LLM call).
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
def search_client(monkeypatch: pytest.MonkeyPatch):
    import app.api.routers.search as search_router_module

    with migrated_schema() as conn:
        monkeypatch.setattr(search_router_module, "get_connection", lambda: contextlib.nullcontext(conn))
        app = create_app()
        app.dependency_overrides[get_current_user] = lambda: USER
        with TestClient(app) as client:
            yield client, conn


def test_search_unauthenticated_returns_401() -> None:
    app = create_app()
    with TestClient(app) as client:
        resp = client.get("/search", params={"q": "spectrum"})
    assert resp.status_code == 401


def test_search_finds_matches_regardless_of_status(search_client) -> None:
    client, conn = search_client
    _insert_article(conn, title="BTRC moves to reprice 2600MHz spectrum", review_status="filtered_out")
    _insert_article(conn, title="Unrelated fuel price story", review_status="approved")
    conn.commit()

    resp = client.get("/search", params={"q": "spectrum"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    assert data["corpusSize"] == 2
    assert data["results"][0]["title"] == "BTRC moves to reprice 2600MHz spectrum"
    assert data["results"][0]["status"] == ""  # filtered_out is never "decided"


def test_search_empty_query_returns_no_results_but_real_corpus_size(search_client) -> None:
    client, conn = search_client
    _insert_article(conn, title="Anything")
    conn.commit()

    resp = client.get("/search", params={"q": ""})
    assert resp.status_code == 200
    data = resp.json()
    assert data["results"] == []
    assert data["total"] == 0
    assert data["corpusSize"] == 1


def test_search_status_label_reflects_approved_and_rejected(search_client) -> None:
    client, conn = search_client
    _insert_article(conn, title="Hormuz oil tanker seized", review_status="approved")
    _insert_article(conn, title="Hormuz cover withdrawn by insurers", review_status="rejected")
    conn.commit()

    resp = client.get("/search", params={"q": "hormuz"})
    statuses = {r["title"]: r["status"] for r in resp.json()["results"]}
    assert statuses["Hormuz oil tanker seized"] == "approved"
    assert statuses["Hormuz cover withdrawn by insurers"] == "rejected"


def test_search_returns_body_when_fetched(search_client) -> None:
    client, conn = search_client
    aid = _insert_article(conn, title="Submarine cable fault reported")
    conn.execute(
        "INSERT INTO article_bodies (article_id, content_text) VALUES (%s, %s)", (aid, "Full article text here.")
    )
    conn.commit()

    resp = client.get("/search", params={"q": "submarine"})
    assert resp.json()["results"][0]["body"] == "Full article text here."
