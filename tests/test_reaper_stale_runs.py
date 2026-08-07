"""Regression tests for app.db.reaper.reap_stale_runs -- the DEPLOY
mission's fix for RT-1-residual (docs/red-team-findings.md).

Companion to tests/test_redteam_orphaned_runs.py (RED-TEAM's file, not
edited by this task): that file's
`test_a_run_that_stops_advancing_its_heartbeat_is_detected_as_stale`
stays `xfail(strict=True)` because one of its two assertions checks that
ADMIN-API's own `app.services.admin.health_runs`/`health_overview` read
path mentions `heartbeat_at` -- that file is outside this task's
owned-files list (app/services/** is P1-INGEST/ADMIN-API's territory
this round), so that specific assertion cannot flip here. What CAN be
verified and regression-tested from this task's own files is the
mechanism RT-1's mission brief actually asked for: a stuck row gets
"reaped or PREVENTED" -- these tests prove `reap_stale_runs` really does
that, called for real from `app.batch.run_daily.main()`.

Postgres-gated (pg_available()), matching every other DB-backed test in
this repo (tests/test_redteam_orphaned_runs.py, tests/test_storage.py, ...).
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.batch.run_daily import main as batch_main
from app.db.reaper import reap_stale_runs
from app.db.testing import migrated_schema, pg_available
from app.services import collector as collector_module
from app.sources.base import FeedItem, FetchedArticle, NewsSource

pg_gated = pytest.mark.skipif(not pg_available(), reason="no reachable Postgres (set TEST_DATABASE_URL)")


class _OneArticleSource(NewsSource):
    key = "reaper_one"
    display_name = "Reaper One"

    def iter_feed_items(self) -> list[FeedItem]:
        return [FeedItem(url="https://reaper.example/a", title="A", source_key=self.key)]

    def fetch_article(self, item: FeedItem) -> FetchedArticle:
        return FetchedArticle(item=item, body_text="")


def _run_row(conn, run_id: int) -> dict:
    return conn.execute(
        "SELECT id, status, finished_at, heartbeat_at, details FROM runs WHERE id = %s", (run_id,)
    ).fetchone()


@pg_gated
def test_reap_stale_runs_marks_an_old_stuck_row_as_error() -> None:
    """The exact scenario RT-1-residual describes: a run that started,
    heartbeated once, then simulates a hard kill (no _finalize_run ever
    runs). Backdating the heartbeat stands in for real elapsed time.
    """
    with migrated_schema() as conn:
        run_id = collector_module._start_run(conn, trigger="scheduled")
        collector_module._touch_heartbeat(conn, run_id)
        conn.execute("UPDATE runs SET heartbeat_at = now() - interval '2 hours' WHERE id = %s", (run_id,))
        conn.commit()

        reaped = reap_stale_runs(conn, stale_after=timedelta(minutes=30))

        assert reaped == [run_id]
        row = _run_row(conn, run_id)
        assert row["status"] == "error"
        assert row["finished_at"] is not None
        assert row["details"]["reaped_by"] == "app.db.reaper.reap_stale_runs"


@pg_gated
def test_reap_stale_runs_uses_started_at_when_heartbeat_never_fired() -> None:
    """A run killed before its FIRST heartbeat (heartbeat_at still NULL)
    must still be reapable via started_at -- the COALESCE in the UPDATE's
    WHERE clause is what this test pins.
    """
    with migrated_schema() as conn:
        run_id = collector_module._start_run(conn, trigger="scheduled")
        # No _touch_heartbeat call at all -- heartbeat_at stays NULL, as
        # it would for a process killed between _start_run and its first
        # completed source.
        conn.execute("UPDATE runs SET started_at = now() - interval '2 hours' WHERE id = %s", (run_id,))
        conn.commit()

        row_before = _run_row(conn, run_id)
        assert row_before["heartbeat_at"] is None

        reaped = reap_stale_runs(conn, stale_after=timedelta(minutes=30))

        assert reaped == [run_id]
        assert _run_row(conn, run_id)["status"] == "error"


@pg_gated
def test_reap_stale_runs_leaves_a_fresh_running_row_alone() -> None:
    """A run genuinely still in progress (heartbeat inside the window)
    must never be touched -- this is the "don't reap a live run" half of
    the contract, not just "do reap a dead one".
    """
    with migrated_schema() as conn:
        run_id = collector_module._start_run(conn, trigger="scheduled")
        collector_module._touch_heartbeat(conn, run_id)

        reaped = reap_stale_runs(conn, stale_after=timedelta(minutes=30))

        assert reaped == []
        row = _run_row(conn, run_id)
        assert row["status"] == "running"
        assert row["finished_at"] is None


@pg_gated
def test_reap_stale_runs_never_touches_a_row_already_done() -> None:
    """A cleanly finished run (status='done'/'error' already) must never
    be re-touched, no matter how old -- the WHERE clause is scoped to
    status='running' only (idx_runs_status_running's own intended query).
    """
    with migrated_schema() as conn:
        run_id = collector_module._start_run(conn, trigger="scheduled")
        collector_module._finalize_run(
            conn, run_id, status="done", total_new=3, total_matched=1, details={"ok": True}
        )
        conn.execute("UPDATE runs SET heartbeat_at = now() - interval '10 hours' WHERE id = %s", (run_id,))
        conn.commit()

        reaped = reap_stale_runs(conn, stale_after=timedelta(minutes=30))

        assert reaped == []
        row = _run_row(conn, run_id)
        assert row["status"] == "done"
        assert row["details"] == {"ok": True}


@pg_gated
def test_batch_main_reaps_a_stale_run_before_starting_its_own(monkeypatch: pytest.MonkeyPatch) -> None:
    """End-to-end through app.batch.run_daily.main() -- the real
    production call path (ADR §1: GitHub Actions cron), not just the
    reaper function in isolation. Same monkeypatch pattern
    tests/test_redteam_orphaned_runs.py::test_batch_main_records_a_runs_row
    uses to swap in a test connection without a real DATABASE_URL.
    """
    with migrated_schema() as conn:
        from contextlib import contextmanager

        import app.batch.run_daily as run_daily_module
        import app.db.connection as connection_module

        @contextmanager
        def _fake_get_connection(dsn: str | None = None):
            yield conn

        monkeypatch.setattr(connection_module, "get_connection", _fake_get_connection)
        monkeypatch.setattr(run_daily_module, "get_connection", _fake_get_connection)
        monkeypatch.setattr(run_daily_module, "all_sources", lambda: {"reaper_one": _OneArticleSource})
        monkeypatch.setattr(run_daily_module, "load_all", lambda: None)
        monkeypatch.setattr(
            run_daily_module,
            "get_settings",
            lambda: type("S", (), {"app_env": "test", "database_url": "postgresql:///fake"})(),
        )
        monkeypatch.setattr(run_daily_module, "validate_production_settings", lambda settings: None)

        stale_run_id = collector_module._start_run(conn, trigger="scheduled")
        collector_module._touch_heartbeat(conn, stale_run_id)
        conn.execute("UPDATE runs SET heartbeat_at = now() - interval '2 hours' WHERE id = %s", (stale_run_id,))
        conn.commit()

        assert batch_main() == 0

        stale_row = _run_row(conn, stale_run_id)
        assert stale_row["status"] == "error", "yesterday's abandoned run should be reaped before today's run starts"

        newest = conn.execute("SELECT status, trigger FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        assert newest["status"] == "done"
        assert newest["trigger"] == "scheduled"
