"""RED-TEAM: mid-run crash / orphaned `runs` rows (mission attack surface
"Mid-run crashes" — legacy-behavior.md §3/§7 item 10: 16 of 110 legacy
runs are permanently stuck at `status='running'` with no `finished_at`,
because nothing in the legacy app ever swept them).

migrations/0002_reference_tables.sql was built specifically to fix this:
a `runs` table with `heartbeat_at`, an `idx_runs_status_running` partial
index, and `app.db.queries.touch_run_heartbeat()` — its own comments
describe the intended watchdog query ("find runs stuck at running") and
call out the exact legacy defect this exists to close.

Finding (see docs/red-team-findings.md, RT-1): nothing in app/ ever
calls any of this. `app.batch.run_daily.main()` — the actual, only batch
entrypoint (app.batch.__main__ imports it, the CI "Batch entrypoint smoke
check" step runs it, ADR §1 wires it to the daily GitHub Actions cron) —
never INSERTs a `runs` row, never calls `touch_run_heartbeat`, and never
marks a run 'done' or 'error'. Grepped across app/: the only callers of
`touch_run_heartbeat` anywhere in the codebase are its own definition and
docstring in app/db/queries.py. `app.services.collector.run_collection`
(the function run_daily.main() calls) never touches the `runs` table
either.

Net effect (AS ORIGINALLY FOUND): it's not that the rebuild reduced
legacy's 16/110 (14.5%) stuck-run rate — it's that 100% of runs were
invisible. There was no row to get stuck, but there was also no row to
prove a run happened, succeeded, or crashed at all. The watchdog schema
existed and was fully inert.

FIXED by P1-INGEST (commit 000576b): `app.services.collector.run_collection`
now inserts a `runs` row up front (committed immediately, independent
of the article-write transaction), touches `heartbeat_at` after each
completed source, and finalizes the row `status='done'`/`'error'` on
the success path or on any exception that survives per-source
isolation. `app.batch.run_daily.main()` passes `trigger="scheduled"`
through. Verified below (`test_run_collection_records_a_runs_row`,
`test_batch_main_records_a_runs_row`) — both flipped from
`xfail(strict=True)` to normal passing regression tests.

RESIDUAL GAP, found while verifying the fix (docs/red-team-findings.md,
RT-1-residual): the fix's durability relies on Python code actually
running — `_start_run`/`_touch_heartbeat`/`_finalize_run` all commit
immediately specifically so a crash SOMEWHERE ELSE still leaves the
right row state. But a hard process kill (SIGKILL, OOM-killer, power
loss) happening while `run_collection()` is between two commits —
including the FIRST heartbeat, i.e. any point after `_start_run`
returns — leaves a `status='running'` row with a heartbeat that will
never advance again, and there is still no consumer anywhere in `app/`
that reads `heartbeat_at` to distinguish that from a run that is
genuinely still in progress: `grep -rn heartbeat_at app/` matches only
the write sites (`app/db/queries.py`'s definition,
`app/services/collector.py`'s two callers) and their own docstrings —
zero read sites, no reaper, no scheduled sweep, no admin-visible "stale
run" flag anywhere, including in `app/services/admin.py`'s
`health_runs()`/`health_overview()` (ADMIN-API's own read path over the
`runs` table), which surfaces `status` verbatim but never compares
`heartbeat_at` against the current time. The exact scenario legacy had
(a `status='running'` row that will sit there forever) still exists
today for this specific failure mode; the fix closes every failure mode
Python's own exception handling can observe, which is a real and
significant improvement, but not the complete "reaps or prevents
orphaned runs" guarantee for a genuine hard kill.

Postgres-gated (pg_available()), matching the pattern every other
DB-backed test in this repo uses (tests/test_auth_api.py Tier 2,
tests/test_storage.py, ...).
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.batch.run_daily import main as batch_main
from app.db.testing import migrated_schema, pg_available
from app.services import collector as collector_module
from app.services.collector import run_collection
from app.sources.base import FeedItem, FetchedArticle, NewsSource

pg_gated = pytest.mark.skipif(not pg_available(), reason="no reachable Postgres (set TEST_DATABASE_URL)")


class _OneArticleSource(NewsSource):
    key = "rt_one"
    display_name = "RT One"

    def iter_feed_items(self) -> list[FeedItem]:
        return [FeedItem(url="https://rt.example/a", title="A", source_key=self.key)]

    def fetch_article(self, item: FeedItem) -> FetchedArticle:
        return FetchedArticle(item=item, body_text="")


def _runs_row_count(conn) -> int:
    row = conn.execute("SELECT count(*) AS n FROM runs").fetchone()
    return int(row["n"]) if row else 0


@pg_gated
def test_run_collection_records_a_runs_row() -> None:
    """RT-1, CONFIRMED FIXED (P1-INGEST, 000576b) -- was xfail(strict=True)."""
    with migrated_schema() as conn:
        before = _runs_row_count(conn)
        run_collection(conn, source_classes={"rt_one": _OneArticleSource}, max_workers=1)
        after = _runs_row_count(conn)
        assert after == before + 1, (
            f"expected run_collection() to insert exactly one `runs` row (before={before}, after={after})"
        )
        row = conn.execute("SELECT status, trigger, total_new FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        assert row["status"] == "done"
        assert row["total_new"] == 1


@pg_gated
def test_batch_main_records_a_runs_row(monkeypatch: pytest.MonkeyPatch) -> None:
    """RT-1, CONFIRMED FIXED (P1-INGEST, 000576b) -- was xfail(strict=True)."""
    with migrated_schema() as conn:
        from contextlib import contextmanager

        import app.batch.run_daily as run_daily_module
        import app.db.connection as connection_module

        @contextmanager
        def _fake_get_connection(dsn: str | None = None):
            yield conn

        monkeypatch.setattr(connection_module, "get_connection", _fake_get_connection)
        monkeypatch.setattr(run_daily_module, "get_connection", _fake_get_connection)
        monkeypatch.setattr(run_daily_module, "all_sources", lambda: {"rt_one": _OneArticleSource})
        monkeypatch.setattr(run_daily_module, "load_all", lambda: None)
        monkeypatch.setattr(
            run_daily_module,
            "get_settings",
            lambda: type("S", (), {"app_env": "test", "database_url": "postgresql:///fake"})(),
        )
        monkeypatch.setattr(run_daily_module, "validate_production_settings", lambda settings: None)

        before = _runs_row_count(conn)
        assert batch_main() == 0
        after = _runs_row_count(conn)
        assert after == before + 1, (
            f"expected a `runs` row to exist after a full batch run (before={before}, after={after})"
        )


# ── RT-1-residual: hard-kill detection and self-healing ─────────────────
# FIXED by DEPLOY (app/db/reaper.py::reap_stale_runs, commit 59dbae7,
# called at the start of every app.batch.run_daily.main()) and
# ADMIN-API (app.services.admin.health_runs surfacing heartbeat_at +
# a computed heartbeat_stale, commit 9315218).


@pg_gated
def test_a_hard_killed_run_is_visible_as_stale_before_the_next_batch_reaps_it() -> None:
    """RT-1-residual, CONFIRMED FIXED -- was xfail(strict=True).

    Simulates a hard kill exactly as before (start + one heartbeat, then
    stop -- no exception, nothing a try/except could observe), backdates
    the heartbeat past the 30-minute threshold, then checks BOTH halves
    of the fix: (1) admin.health_runs marks it heartbeat_stale=True
    before anything reaps it, using the SAME 30-minute threshold
    app.db.reaper uses, and (2) the next app.batch.run_daily.main()
    invocation self-heals it to status='error' before starting its own
    run, per reap_stale_runs' own "prevents, not just reaps after the
    fact" design.
    """
    from app.db.reaper import DEFAULT_STALE_AFTER, reap_stale_runs
    from app.services import admin as admin_module

    with migrated_schema() as conn:
        run_id = collector_module._start_run(conn, trigger="scheduled")
        collector_module._touch_heartbeat(conn, run_id)
        # <-- a real SIGKILL happens here; no further Python code runs.

        conn.execute(
            "UPDATE runs SET heartbeat_at = now() - interval '2 hours' WHERE id = %s",
            (run_id,),
        )
        conn.commit()

        # (1) Visible as stale via the admin read path, same threshold
        # the reaper itself uses -- not just detectable by hand-writing
        # SQL, but by the actual application code.
        assert DEFAULT_STALE_AFTER == timedelta(minutes=30)
        rows = admin_module.health_runs(conn, limit=10)
        this_row = next(r for r in rows if r["id"] == run_id)
        assert this_row["status"] == "running"
        assert this_row["heartbeat_stale"] is True, "a 2-hour-stale heartbeat must be flagged stale"

        # (2) Self-heals: the next batch run reaps it before touching
        # the runs table again (this IS what app.batch.run_daily.main()
        # does at its own top, exercised directly here at the function
        # reaper.py itself provides).
        reaped_ids = reap_stale_runs(conn)
        assert run_id in reaped_ids
        row = conn.execute("SELECT status, finished_at FROM runs WHERE id = %s", (run_id,)).fetchone()
        assert row["status"] == "error"
        assert row["finished_at"] is not None

        # Re-checking health_runs after the reap: no longer flagged
        # stale (it's not 'running' anymore, it's a finalized 'error').
        rows_after = admin_module.health_runs(conn, limit=10)
        this_row_after = next(r for r in rows_after if r["id"] == run_id)
        assert this_row_after["heartbeat_stale"] is False


@pg_gated
def test_healthy_running_run_is_not_flagged_stale_or_reaped(monkeypatch: pytest.MonkeyPatch) -> None:
    """Control: a run that started seconds ago with a fresh heartbeat
    must NOT be flagged stale or reaped -- the threshold must not be so
    aggressive it kills a live, in-progress run out from under itself.
    """
    from app.db.reaper import reap_stale_runs
    from app.services import admin as admin_module

    with migrated_schema() as conn:
        run_id = collector_module._start_run(conn, trigger="scheduled")
        collector_module._touch_heartbeat(conn, run_id)

        rows = admin_module.health_runs(conn, limit=10)
        this_row = next(r for r in rows if r["id"] == run_id)
        assert this_row["heartbeat_stale"] is False

        reaped_ids = reap_stale_runs(conn)
        assert run_id not in reaped_ids
        row = conn.execute("SELECT status FROM runs WHERE id = %s", (run_id,)).fetchone()
        assert row["status"] == "running"
