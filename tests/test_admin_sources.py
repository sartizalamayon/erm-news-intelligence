"""Source CRUD + live test/diagnose probes (app.services.admin,
app.api.routers.sources). Service-layer tests run against a real,
migrated Postgres schema (app.db.testing.migrated_schema); router-level
tests additionally prove the HTTP wiring (404s, admin-session cookie
flow) for a representative slice, following tests/test_auth_api.py's
Tier-1/Tier-2 split.

Probe/diagnose tests never touch the real `app.sources` plugin registry
or the network — `app.services.admin._plugin_for_source_name` is
monkeypatched to return a small fake `NewsSource` per test, exercising
admin.py's own dispatch/aggregation logic in isolation.
"""

from __future__ import annotations

import contextlib
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import app.api.deps as deps_module
from app.api.deps import AuthenticatedUser, get_current_user
from app.db.testing import migrated_schema, pg_available
from app.errors import SourceError, ValidationError
from app.main import create_app
from app.services import admin as admin_service
from app.sources.base import FeedItem, FetchedArticle, NewsSource

pytestmark = pytest.mark.skipif(not pg_available(), reason="no reachable Postgres (set TEST_DATABASE_URL)")

ADMIN = AuthenticatedUser(id=9, email="admin@grameenphone.com", role="admin", display_name="Admin", session_id=9)


# ── Service-layer CRUD ────────────────────────────────────────────────────


def test_create_list_get_source_round_trips_methods_as_list() -> None:
    with migrated_schema() as conn:
        created = admin_service.create_source(
            conn, name="Kalbela", methods=["sitemap", "rss"], language="bn", enabled=True
        )
        assert created["methods"] == ["sitemap", "rss"]
        assert created["language"] == "bn"
        assert created["enabled"] is True

        fetched = admin_service.get_source(conn, created["id"])
        assert fetched == created

        listed = admin_service.list_sources(conn)
        assert [s["name"] for s in listed] == ["Kalbela"]


def test_create_source_duplicate_name_raises_validation_error() -> None:
    with migrated_schema() as conn:
        admin_service.create_source(conn, name="Prothom Alo", methods=["api"])
        with pytest.raises(ValidationError):
            admin_service.create_source(conn, name="Prothom Alo", methods=["rss"])


def test_create_source_requires_at_least_one_method() -> None:
    with migrated_schema() as conn:
        with pytest.raises(ValidationError):
            admin_service.create_source(conn, name="No Method Outlet", methods=[])


def test_update_source_partial_fields_only_changes_given_columns() -> None:
    with migrated_schema() as conn:
        created = admin_service.create_source(conn, name="Daily Star", methods=["sitemap"], language="en")
        updated = admin_service.update_source(conn, created["id"], enabled=False)
        assert updated is not None
        assert updated["enabled"] is False
        assert updated["name"] == "Daily Star"
        assert updated["methods"] == ["sitemap"]


def test_update_source_unknown_id_returns_none() -> None:
    with migrated_schema() as conn:
        assert admin_service.update_source(conn, 999999, enabled=False) is None


def test_toggle_source_flips_enabled() -> None:
    with migrated_schema() as conn:
        created = admin_service.create_source(conn, name="New Age", methods=["rss"], enabled=True)
        toggled = admin_service.toggle_source(conn, created["id"])
        assert toggled is not None
        assert toggled["enabled"] is False
        toggled_again = admin_service.toggle_source(conn, created["id"])
        assert toggled_again is not None
        assert toggled_again["enabled"] is True


def test_delete_source_removes_row_and_reports_result() -> None:
    with migrated_schema() as conn:
        created = admin_service.create_source(conn, name="BD Pratidin", methods=["sitemap"])
        assert admin_service.delete_source(conn, created["id"]) is True
        assert admin_service.get_source(conn, created["id"]) is None
        assert admin_service.delete_source(conn, created["id"]) is False


# ── Live probes (fake plugin, no network) ────────────────────────────────


class _FakeSource(NewsSource):
    key = "fake_outlet"
    display_name = "Fake Outlet"

    def __init__(self, items=None, feed_error=None, article_error=None, body_text="some article body") -> None:
        self._items = items if items is not None else []
        self._feed_error = feed_error
        self._article_error = article_error
        self._body_text = body_text

    def iter_feed_items(self) -> list[FeedItem]:
        if self._feed_error:
            raise self._feed_error
        return self._items

    def fetch_article(self, item: FeedItem) -> FetchedArticle:
        if self._article_error:
            raise self._article_error
        return FetchedArticle(item=item, body_text=self._body_text)


def _now_dhaka() -> datetime:
    # A real, correctly-labeled UTC instant -- probe_source's own
    # `.astimezone(dhaka)` does the +6h conversion; shifting here too
    # double-applies the offset and can roll the date over.
    return datetime.now(timezone.utc)


def test_probe_source_unknown_id_returns_none() -> None:
    with migrated_schema() as conn:
        assert admin_service.probe_source(conn, 999999) is None


def test_probe_source_no_registered_plugin_reports_not_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(admin_service, "_plugin_for_source_name", lambda name: None)
    with migrated_schema() as conn:
        created = admin_service.create_source(conn, name="Unmapped Outlet", methods=["rss"])
        result = admin_service.probe_source(conn, created["id"])
        assert result is not None
        assert result["ok"] is False
        assert "no fetch plugin registered" in result["detail"]


def test_probe_source_counts_fetched_and_today(monkeypatch: pytest.MonkeyPatch) -> None:
    today = _now_dhaka()
    items = [
        FeedItem(url="https://x.test/1", title="A", source_key="fake_outlet", published_at=today),
        FeedItem(url="https://x.test/2", title="B", source_key="fake_outlet", published_at=today - timedelta(days=3)),
        FeedItem(url="https://x.test/3", title="C", source_key="fake_outlet", published_at=None),
    ]
    fake_cls = lambda items=items: _FakeSource(items=items)  # noqa: E731
    monkeypatch.setattr(admin_service, "_plugin_for_source_name", lambda name: fake_cls)
    with migrated_schema() as conn:
        created = admin_service.create_source(conn, name="Sitemap Outlet", methods=["sitemap"])
        result = admin_service.probe_source(conn, created["id"])
        assert result["ok"] is True
        assert result["fetched"] == 3
        assert result["today"] == 1
        assert "3 fetched, 1 from today" in result["detail"]


def test_probe_source_fetch_failure_reports_not_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_cls = lambda: _FakeSource(feed_error=SourceError("boom", source="fake_outlet"))  # noqa: E731
    monkeypatch.setattr(admin_service, "_plugin_for_source_name", lambda name: fake_cls)
    with migrated_schema() as conn:
        created = admin_service.create_source(conn, name="Flaky Outlet", methods=["api"])
        result = admin_service.probe_source(conn, created["id"])
        assert result["ok"] is False
        assert "boom" in result["detail"]


def test_diagnose_source_reports_each_check(monkeypatch: pytest.MonkeyPatch) -> None:
    items = [FeedItem(url="https://x.test/1", title="A", source_key="fake_outlet", published_at=_now_dhaka())]
    fake_cls = lambda: _FakeSource(items=items, body_text="full article text")  # noqa: E731
    monkeypatch.setattr(admin_service, "_plugin_for_source_name", lambda name: fake_cls)
    with migrated_schema() as conn:
        created = admin_service.create_source(conn, name="Healthy Outlet", methods=["rss"])
        result = admin_service.diagnose_source(conn, created["id"])
        assert result["reachable"] is True
        assert result["feed_discovery_ok"] is True
        assert result["article_url_found"] is True
        assert result["date_parse_ok"] is True


def test_diagnose_source_no_publish_date_flags_date_parse_false(monkeypatch: pytest.MonkeyPatch) -> None:
    items = [FeedItem(url="https://x.test/1", title="A", source_key="fake_outlet", published_at=None)]
    fake_cls = lambda: _FakeSource(items=items)  # noqa: E731
    monkeypatch.setattr(admin_service, "_plugin_for_source_name", lambda name: fake_cls)
    with migrated_schema() as conn:
        created = admin_service.create_source(conn, name="Dateless Outlet", methods=["rss"])
        result = admin_service.diagnose_source(conn, created["id"])
        assert result["date_parse_ok"] is False
        assert "date parse" in result["detail"]


# ── Router-level: 404s and the admin-session cookie flow ─────────────────


@pytest.fixture
def admin_client(monkeypatch: pytest.MonkeyPatch):
    """A TestClient authenticated as an admin via dependency override —
    same pattern as tests/test_auth_api.py's role-enforcement tests, but
    for the SUCCESS path this time (admin allowed through) — AND wired
    to a real migrated schema (app.db.testing.migrated_schema), since
    every route past the require_admin gate opens a DB connection via
    app.api.routers.sources.get_connection.
    """
    import app.api.routers.sources as sources_router_module

    with migrated_schema() as conn:
        monkeypatch.setattr(sources_router_module, "get_connection", lambda: contextlib.nullcontext(conn))
        app = create_app()
        app.dependency_overrides[get_current_user] = lambda: ADMIN
        with TestClient(app) as test_client:
            yield test_client


def test_router_update_unknown_source_returns_404(admin_client: TestClient) -> None:
    resp = admin_client.patch("/sources/999999", json={"enabled": False})
    assert resp.status_code == 404


def test_router_delete_unknown_source_returns_404(admin_client: TestClient) -> None:
    resp = admin_client.delete("/sources/999999")
    assert resp.status_code == 404


def test_router_test_unknown_source_returns_404(admin_client: TestClient) -> None:
    resp = admin_client.post("/sources/999999/test")
    assert resp.status_code == 404


def test_router_create_duplicate_name_returns_422(monkeypatch: pytest.MonkeyPatch) -> None:
    with migrated_schema() as conn:
        monkeypatch.setattr(deps_module, "get_connection", lambda: contextlib.nullcontext(conn))
        import app.api.routers.sources as sources_router_module

        monkeypatch.setattr(sources_router_module, "get_connection", lambda: contextlib.nullcontext(conn))
        admin_service.create_source(conn, name="Dhaka Post", methods=["sitemap"])

        app = create_app()
        app.dependency_overrides[get_current_user] = lambda: ADMIN
        with TestClient(app) as client:
            resp = client.post("/sources", json={"name": "Dhaka Post", "methods": ["rss"]})
        assert resp.status_code == 422


def test_router_full_crud_flow_end_to_end(monkeypatch: pytest.MonkeyPatch) -> None:
    with migrated_schema() as conn:
        import app.api.routers.sources as sources_router_module

        monkeypatch.setattr(sources_router_module, "get_connection", lambda: contextlib.nullcontext(conn))

        app = create_app()
        app.dependency_overrides[get_current_user] = lambda: ADMIN
        with TestClient(app) as client:
            created = client.post("/sources", json={"name": "Nagorik", "methods": ["rss", "sitemap"]})
            assert created.status_code == 201
            source_id = created.json()["id"]
            assert created.json()["methods"] == ["rss", "sitemap"]

            listed = client.get("/sources")
            assert listed.status_code == 200
            assert any(s["id"] == source_id for s in listed.json())

            toggled = client.post(f"/sources/{source_id}/toggle")
            assert toggled.status_code == 200
            assert toggled.json()["enabled"] is False

            updated = client.patch(f"/sources/{source_id}", json={"language": "en"})
            assert updated.status_code == 200
            assert updated.json()["language"] == "en"

            deleted = client.delete(f"/sources/{source_id}")
            assert deleted.status_code == 204

            gone = client.patch(f"/sources/{source_id}", json={"enabled": True})
            assert gone.status_code == 404
