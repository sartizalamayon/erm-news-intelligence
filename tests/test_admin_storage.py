"""Storage dashboard + purge + export/import
(app.services.admin, app.api.routers.dashboard).

app.services.storage/app.services.data_transfer's own dry-run/confirm
and export-first-gate semantics are exhaustively covered by
tests/test_storage.py and tests/test_storage_transfer.py -- this file
proves app.services.admin's thin dispatch layer over them (operation
routing, ISO-date parsing at the router boundary, dataclass -> pydantic
shape) and the HTTP wiring, not the purge/export logic itself.
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


def test_storage_usage_reflects_real_introspection() -> None:
    with migrated_schema() as conn:
        insert_article(
            conn, url="https://x.test/1", url_norm="https://x.test/1", source="X", title="A", day=dt.date.today()
        )
        conn.commit()
        usage = admin_service.storage_usage(conn)
        assert usage.database_size_bytes > 0
        assert usage.ceiling_bytes == 500 * 1024 * 1024


def test_run_purge_dispatches_to_the_right_operation_and_defaults_to_dry_run() -> None:
    with migrated_schema() as conn:
        article_id = insert_article(
            conn,
            url="https://x.test/1",
            url_norm="https://x.test/1",
            source="X",
            title="A",
            day=dt.date.today() - dt.timedelta(days=400),
            review_status="filtered_out",
        )
        conn.commit()

        preview = admin_service.run_purge(conn, operation="stale_unmatched", older_than_days=90, confirm=False)
        assert preview.confirmed is False
        assert preview.rows_affected == 1
        still_there = conn.execute("SELECT 1 FROM articles WHERE id = %s", (article_id,)).fetchone()
        assert still_there is not None  # dry run must not mutate

        confirmed = admin_service.run_purge(conn, operation="stale_unmatched", older_than_days=90, confirm=True)
        conn.commit()
        assert confirmed.confirmed is True
        assert confirmed.rows_affected == 1


def test_run_purge_matched_requires_older_than_date() -> None:
    with migrated_schema() as conn:
        with pytest.raises(ValidationError):
            admin_service.run_purge(conn, operation="matched", confirm=False)


def test_run_purge_unknown_operation_raises_validation_error() -> None:
    with migrated_schema() as conn:
        with pytest.raises(ValidationError):
            admin_service.run_purge(conn, operation="not_a_real_operation", confirm=False)


def test_export_then_import_round_trips(tmp_path) -> None:
    with migrated_schema() as conn:
        article_id = insert_article(
            conn,
            url="https://x.test/export-me",
            url_norm="https://x.test/export-me",
            source="X",
            title="Exportable",
            day=dt.date.today(),
            review_status="approved",
        )
        conn.commit()

        export_path = str(tmp_path / "export.jsonl.gz")
        manifest = admin_service.run_export(conn, path=export_path)
        assert manifest.articles_exported == 1

        exported_at = conn.execute("SELECT exported_at FROM articles WHERE id = %s", (article_id,)).fetchone()
        assert exported_at["exported_at"] is not None

        # Purge it (export-gated) then re-import to prove the full journey.
        # ARCH-DB's RT-6 fix requires a matching confirm=False preview
        # snapshot before confirm=True is honored (app.services.storage's
        # module docstring, "Closing RT-6") — same (operation, params).
        purge_kwargs = dict(
            operation="matched", older_than=dt.date.today() + dt.timedelta(days=1), require_exported=True
        )
        admin_service.run_purge(conn, confirm=False, **purge_kwargs)
        purge_result = admin_service.run_purge(conn, confirm=True, **purge_kwargs)
        conn.commit()
        assert purge_result.rows_affected == 1
        assert conn.execute("SELECT 1 FROM articles WHERE id = %s", (article_id,)).fetchone() is None

        report = admin_service.run_import(conn, path=export_path)
        assert report.articles_imported == 1
        restored = conn.execute("SELECT id, title FROM articles WHERE id = %s", (article_id,)).fetchone()
        assert restored is not None
        assert restored["title"] == "Exportable"


# ── Router-level ──────────────────────────────────────────────────────


def test_router_storage_and_purge_dry_run_end_to_end(monkeypatch: pytest.MonkeyPatch) -> None:
    with migrated_schema() as conn:
        import app.api.routers.dashboard as dashboard_router_module

        insert_article(
            conn, url="https://x.test/1", url_norm="https://x.test/1", source="X", title="A", day=dt.date.today()
        )
        conn.commit()

        monkeypatch.setattr(dashboard_router_module, "get_connection", lambda: contextlib.nullcontext(conn))

        app = create_app()
        app.dependency_overrides[get_current_user] = lambda: ADMIN
        with TestClient(app) as client:
            usage = client.get("/dashboard/storage")
            assert usage.status_code == 200
            assert usage.json()["ceiling_bytes"] == 500 * 1024 * 1024

            purge = client.post("/dashboard/storage/purge", json={"operation": "stale_unmatched", "confirm": False})
            assert purge.status_code == 200
            body = purge.json()
            assert body["confirmed"] is False
            assert body["operation"] == "purge_stale_unmatched_articles"

            bad = client.post("/dashboard/storage/purge", json={"operation": "matched", "confirm": False})
            assert bad.status_code == 422


def test_router_export_import_end_to_end(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    with migrated_schema() as conn:
        import app.api.routers.dashboard as dashboard_router_module

        insert_article(
            conn,
            url="https://x.test/export",
            url_norm="https://x.test/export",
            source="X",
            title="A",
            day=dt.date.today(),
        )
        conn.commit()

        monkeypatch.setattr(dashboard_router_module, "get_connection", lambda: contextlib.nullcontext(conn))
        export_path = str(tmp_path / "router_export.jsonl.gz")

        app = create_app()
        app.dependency_overrides[get_current_user] = lambda: ADMIN
        with TestClient(app) as client:
            export_resp = client.post("/dashboard/storage/export", json={"path": export_path})
            assert export_resp.status_code == 200
            assert export_resp.json()["articles_exported"] == 1

            import_resp = client.post("/dashboard/storage/import", json={"path": export_path})
            assert import_resp.status_code == 200
            assert import_resp.json()["articles_skipped_existing"] == 1  # already present, idempotent
