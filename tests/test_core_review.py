"""Tests for the Review queue (frontend-contract.md §3.3): GET /articles,
GET /articles/{id}, PATCH /articles/{id}, POST /articles/bulk,
POST /articles/{id}/dups/{dup_id}/keep|drop.

REVIEW-REBUILD: the list endpoint (`GET /articles`) is now a lightweight,
paginated shape (no body/dups/topicPeers/note) — see app/api/routers/
review.py's module docstring for the measured performance reasons.
`GET /articles/{id}` is the new on-demand full-detail endpoint. Several
tests below assert the SHAPE split explicitly (list rows carry no body
text; only the detail endpoint does) since that split is the load-bearing
perf fix, not incidental.
"""

from __future__ import annotations

import contextlib
import itertools
import time
from datetime import date, timedelta

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
    if "content_text" in defaults:
        content_text = defaults.pop("content_text")
    else:
        content_text = None
    columns = list(defaults.keys())
    placeholders = ", ".join(f"%({c})s" for c in columns)
    row = conn.execute(
        f"INSERT INTO articles ({', '.join(columns)}) VALUES ({placeholders}) RETURNING id", defaults
    ).fetchone()
    aid = row["id"]
    if content_text is not None:
        conn.execute(
            "INSERT INTO article_bodies (article_id, content_text) VALUES (%s, %s)", (aid, content_text)
        )
    return aid


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


def test_get_article_unauthenticated_returns_401() -> None:
    app = create_app()
    with TestClient(app) as client:
        resp = client.get("/articles/1")
    assert resp.status_code == 401


def test_patch_article_unauthenticated_returns_401() -> None:
    app = create_app()
    with TestClient(app) as client:
        resp = client.patch("/articles/1", json={"status": "approved"})
    assert resp.status_code == 401


def test_list_excludes_filtered_out_and_includes_pending(review_client) -> None:
    client, conn = review_client
    _insert_article(
        conn, title="Matched one", sections="Macroeconomy", keywords="inflation", llm_score=8, review_status="pending"
    )
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
    assert item["date"] == date.today().isoformat()


def test_list_rows_are_lightweight_no_body_or_dup_detail(review_client) -> None:
    """The measured perf fix: list rows never carry body text or full
    dup/topic detail, even for an article that HAS a body and a dup
    group — see the module docstring's before/after numbers.
    """
    client, conn = review_client
    group = 777
    _insert_article(
        conn, title="Has body", sections="Others", llm_score=9, content_text="a" * 3000, dup_group=group,
    )
    _insert_article(conn, title="Sibling", sections="Others", dup_group=group)
    conn.commit()

    resp = client.get("/articles")
    item = next(a for a in resp.json()["items"] if a["title"] == "Has body")
    assert item["body"] == ""
    assert item["dups"] is None
    assert item["topicPeers"] is None
    assert item["keywords"] == []
    # ...but the list-level dup COUNT still reflects it (list rows show a "+N" pill).
    assert item["dupCount"] == 1


def test_get_article_detail_includes_body_and_dups(review_client) -> None:
    client, conn = review_client
    group = 888
    aid = _insert_article(
        conn, title="Full detail", sections="Others", llm_score=7, content_text="body text here", dup_group=group,
    )
    _insert_article(conn, title="Sibling", source="Daily Star", sections="Others", dup_group=group)
    conn.commit()

    resp = client.get(f"/articles/{aid}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["body"] == "body text here"
    assert body["dups"] is not None
    assert len(body["dups"]) == 1
    assert body["dups"][0]["source"] == "Daily Star"


def test_get_article_unknown_id_returns_404(review_client) -> None:
    client, _conn = review_client
    resp = client.get("/articles/999999")
    assert resp.status_code == 404


def test_list_pagination_limit_and_offset(review_client) -> None:
    client, conn = review_client
    for i in range(5):
        _insert_article(conn, title=f"Row {i}", sections="Others", llm_score=5 + i)
    conn.commit()

    first = client.get("/articles", params={"limit": 2, "offset": 0}).json()
    assert len(first["items"]) == 2
    assert first["total"] == 5

    second = client.get("/articles", params={"limit": 2, "offset": 2}).json()
    assert len(second["items"]) == 2
    assert {a["id"] for a in first["items"]}.isdisjoint({a["id"] for a in second["items"]})


def test_list_top_n_mode_overrides_normal_limit(review_client) -> None:
    client, conn = review_client
    for i in range(5):
        _insert_article(conn, title=f"Row {i}", sections="Others", llm_score=i + 1)
    conn.commit()

    resp = client.get("/articles", params={"top_n": 3, "limit": 1})
    items = resp.json()["items"]
    assert len(items) == 3
    # relevance order regardless of any other sort requested
    assert [a["score"] for a in items] == sorted((a["score"] for a in items), reverse=True)


def test_list_score_range_filter(review_client) -> None:
    client, conn = review_client
    _insert_article(conn, title="Low", sections="Others", llm_score=3)
    _insert_article(conn, title="Mid", sections="Others", llm_score=6)
    _insert_article(conn, title="High", sections="Others", llm_score=9)
    conn.commit()

    resp = client.get("/articles", params={"score_min": 5, "score_max": 7})
    titles = [a["title"] for a in resp.json()["items"]]
    assert titles == ["Mid"]


def test_list_date_range_filter(review_client) -> None:
    client, conn = review_client
    today = date.today()
    _insert_article(conn, title="Today", sections="Others", day=today)
    _insert_article(conn, title="Old", sections="Others", day=today - timedelta(days=10))
    conn.commit()

    resp = client.get("/articles", params={"date_from": today.isoformat(), "date_to": today.isoformat()})
    titles = [a["title"] for a in resp.json()["items"]]
    assert titles == ["Today"]


def test_list_status_filter_shows_only_pending_by_default_when_status_given(review_client) -> None:
    client, conn = review_client
    _insert_article(conn, title="Pending one", sections="Others", review_status="pending")
    _insert_article(conn, title="Approved one", sections="Others", review_status="approved")
    conn.commit()

    resp = client.get("/articles", params={"status": "pending"})
    assert resp.status_code == 200
    titles = [a["title"] for a in resp.json()["items"]]
    assert titles == ["Pending one"]

    # "Approved only" is a first-class, distinct filter value (not "everything decided").
    resp_approved = client.get("/articles", params={"status": "approved"})
    titles_approved = [a["title"] for a in resp_approved.json()["items"]]
    assert titles_approved == ["Approved one"]

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


def test_list_sources_facet_counts_ignore_exclude_source_and_starred(review_client) -> None:
    """Faceted-search semantics: a source's own count, and the total,
    must stay visible even once you've filtered it out or asked for
    starred-only — otherwise the sidebar checkbox list can't be
    un-toggled sanely. (Matches the pre-rebuild behavior.)
    """
    client, conn = review_client
    a1 = _insert_article(conn, title="A", source="Daily Star", sections="Others")
    _insert_article(conn, title="B", source="Prothom Alo", sections="Others")
    conn.commit()
    client.patch(f"/articles/{a1}", json={"starred": True})

    resp = client.get("/articles", params={"exclude_source": "Daily Star", "starred": "true"})
    sources = {s["name"]: s["count"] for s in resp.json()["sources"]}
    assert sources == {"Daily Star": 1, "Prothom Alo": 1}
    # but the actual items list DOES honor both filters
    titles = [a["title"] for a in resp.json()["items"]]
    assert titles == []


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


def test_patch_star_persists_and_shows_in_list(review_client) -> None:
    client, conn = review_client
    aid = _insert_article(conn, title="X", sections="Others")
    conn.commit()

    resp = client.patch(f"/articles/{aid}", json={"starred": True})
    assert resp.status_code == 200
    assert resp.json()["isStarred"] is True

    # isStarred round-trips on a list fetch too (list rows keep this field —
    # only body/dups/topicPeers/note are dropped from the list shape).
    listed = client.get("/articles").json()["items"]
    item = next(a for a in listed if a["id"] == aid)
    assert item["isStarred"] is True


def test_patch_note_persists_via_detail_endpoint_not_list(review_client) -> None:
    """REVIEW-REBUILD: the review SCREEN no longer sends/reads notes
    (owner's explicit "remove the note feature" call), but the backend
    PATCH/settings-table plumbing is left intact rather than deleted. It
    round-trips via the detail endpoint; the lightweight list endpoint
    deliberately never carries it (see test_list_rows_are_lightweight...).
    """
    client, conn = review_client
    aid = _insert_article(conn, title="X", sections="Others")
    conn.commit()

    resp = client.patch(f"/articles/{aid}", json={"note": "watch this one"})
    assert resp.status_code == 200
    assert resp.json()["note"] == "watch this one"

    detail = client.get(f"/articles/{aid}").json()
    assert detail["note"] == "watch this one"

    listed = client.get("/articles").json()["items"]
    item = next(a for a in listed if a["id"] == aid)
    assert item["note"] == ""

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


def test_bulk_approve_at_scale_stays_fast(review_client) -> None:
    """Owner's explicit concern: bulk approve must work at 50+ selected
    without hanging the UI. This is the backend half of that guarantee —
    a single batched UPDATE, not a loop — asserted via a generous wall
    clock ceiling so a real regression (e.g. someone reintroducing a
    per-id loop) fails this test.
    """
    client, conn = review_client
    ids = [_insert_article(conn, title=f"Bulk {i}", sections="Others") for i in range(60)]
    conn.commit()

    t0 = time.time()
    resp = client.post("/articles/bulk", json={"ids": ids, "action": "approve"})
    elapsed = time.time() - t0

    assert resp.status_code == 200
    assert resp.json()["updated"] == 60
    assert elapsed < 2.0, f"bulk approve of 60 ids took {elapsed:.2f}s — investigate for a reintroduced per-id loop"


def _insert_dup_pair(conn, group: int) -> tuple[int, int]:
    primary = _insert_article(
        conn, title="Original", source="Daily Star", sections="Others", dup_group=group, is_primary=True
    )
    sibling = _insert_article(
        conn, title="Duplicate", source="The Business Standard", sections="Others",
        dup_group=group, is_primary=False,
    )
    return primary, sibling


def test_dup_keep_promotes_the_chosen_sibling_to_primary(review_client) -> None:
    client, conn = review_client
    group = 4242
    primary, sibling = _insert_dup_pair(conn, group)
    conn.commit()

    resp = client.post(f"/articles/{primary}/dups/{sibling}/keep")
    assert resp.status_code == 200
    assert resp.json()["id"] == sibling

    dup_rows = conn.execute(
        "SELECT id, is_primary, also_in FROM articles WHERE dup_group = %s", (group,)
    ).fetchall()
    rows = {r["id"]: (r["is_primary"], r["also_in"]) for r in dup_rows}
    assert rows[sibling][0] is True
    assert rows[sibling][1] == "Daily Star"
    assert rows[primary][0] is False
    assert rows[primary][1] == ""


def test_dup_drop_removes_sibling_from_group(review_client) -> None:
    client, conn = review_client
    group = 4343
    primary, sibling = _insert_dup_pair(conn, group)
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
