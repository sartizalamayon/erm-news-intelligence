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


# ── SEARCH-FILTERS: date range / source / language / status, combinable ──


def test_search_date_range_filter(search_client) -> None:
    client, conn = search_client
    _insert_article(conn, title="Spectrum auction old news", day=date(2020, 1, 1))
    _insert_article(conn, title="Spectrum auction fresh news", day=date(2026, 1, 1))
    conn.commit()

    resp = client.get("/search", params={"q": "spectrum", "date_from": "2025-01-01"})
    titles = {r["title"] for r in resp.json()["results"]}
    assert titles == {"Spectrum auction fresh news"}


def test_search_source_exclude_filter(search_client) -> None:
    client, conn = search_client
    _insert_article(conn, title="Tariff hike story", source="Prothom Alo")
    _insert_article(conn, title="Tariff hike coverage", source="Daily Star")
    conn.commit()

    resp = client.get("/search", params={"q": "tariff", "exclude_source": "Daily Star"})
    sources = {r["source"] for r in resp.json()["results"]}
    assert sources == {"Prothom Alo"}


def test_search_language_filter(search_client) -> None:
    client, conn = search_client
    _insert_article(conn, title="Hormuz strait english title")
    _insert_article(conn, title="হরমুজ প্রণালী hormuz bangla title")
    conn.commit()

    resp = client.get("/search", params={"q": "hormuz", "lang": "bn"})
    results = resp.json()["results"]
    assert len(results) == 1
    assert "হরমুজ" in results[0]["title"]


def test_search_status_filter_covers_never_matched(search_client) -> None:
    """Owner's ask: search covers articles that were never keyword-
    matched (and thus never AI-scored) too — `status=never_matched`
    maps to review_status='filtered_out'.
    """
    client, conn = search_client
    _insert_article(conn, title="Cyclone warning approved", review_status="approved", match_type="keyword")
    _insert_article(conn, title="Cyclone warning pending", review_status="pending", match_type="keyword")
    _insert_article(conn, title="Cyclone warning never matched", review_status="filtered_out", match_type="")
    conn.commit()

    resp = client.get("/search", params={"q": "cyclone", "status": "never_matched"})
    results = resp.json()["results"]
    assert len(results) == 1
    assert results[0]["title"] == "Cyclone warning never matched"

    resp2 = client.get("/search", params={"q": "cyclone", "status": "matched"})
    assert [r["title"] for r in resp2.json()["results"]] == ["Cyclone warning pending"]


def test_search_filters_are_combinable(search_client) -> None:
    client, conn = search_client
    _insert_article(
        conn, title="Grid outage report A", source="BBC News", day=date(2026, 1, 1), review_status="approved"
    )
    _insert_article(
        conn, title="Grid outage report B", source="BBC News", day=date(2020, 1, 1), review_status="approved"
    )
    _insert_article(
        conn, title="Grid outage report C", source="Daily Star", day=date(2026, 1, 1), review_status="approved"
    )
    conn.commit()

    resp = client.get(
        "/search",
        params={"q": "grid outage", "date_from": "2025-01-01", "exclude_source": "Daily Star", "status": "approved"},
    )
    titles = {r["title"] for r in resp.json()["results"]}
    assert titles == {"Grid outage report A"}


def test_search_pagination_is_bounded(search_client) -> None:
    client, conn = search_client
    for i in range(25):
        _insert_article(conn, title=f"Bounded query test article {i}")
    conn.commit()

    resp = client.get("/search", params={"q": "bounded"})
    data = resp.json()
    assert data["total"] == 25
    assert len(data["results"]) == 20  # PAGE_SIZE — never the full 25 in one response

    resp_p2 = client.get("/search", params={"q": "bounded", "page": 2})
    assert len(resp_p2.json()["results"]) == 5


def test_search_returns_source_facet(search_client) -> None:
    client, conn = search_client
    _insert_article(conn, title="Facet check story one", source="Prothom Alo")
    _insert_article(conn, title="Facet check story two", source="Prothom Alo")
    _insert_article(conn, title="Facet check story three", source="Daily Star")
    conn.commit()

    resp = client.get("/search", params={"q": "facet check"})
    sources = {s["name"]: s["count"] for s in resp.json()["sources"]}
    assert sources == {"Prothom Alo": 2, "Daily Star": 1}
