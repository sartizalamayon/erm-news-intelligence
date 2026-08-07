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


# ── RT-1-residual: the fix closes every CAUGHT-exception crash, not a ──
# ── genuine hard kill (SIGKILL/OOM/power loss) ──────────────────────────


@pg_gated
@pytest.mark.xfail(
    strict=True,
    reason="RT-1-residual (docs/red-team-findings.md): a hard process kill between "
    "_start_run() and _finalize_run() -- not simulable by raising a Python exception, which "
    "IS handled -- leaves a status='running' row with a heartbeat that stops advancing, and "
    "nothing anywhere in app/ (no reaper, no scheduled sweep, no admin health-check comparison "
    "of heartbeat_at against now()) ever surfaces that as stale. Owner: P1-INGEST (write side "
    "already fixed) / open for whoever builds the watchdog consumer.",
)
def test_a_run_that_stops_advancing_its_heartbeat_is_detected_as_stale() -> None:
    """Simulates a hard kill: start a run and touch its heartbeat once
    (exactly what a real run does before any work begins), then -- since
    an OS-level SIGKILL cannot be simulated by raising a Python
    exception (that path IS handled by run_collection()'s own
    try/except, verified fixed above) -- simply stop, as a killed
    process would. No further code runs. The row is now indistinguishable
    from a live run except that its heartbeat has gone stale.

    Asserts the property RT-1's own mission brief actually asked for:
    the rebuild "reaps or PREVENTS" orphaned runs. This checks for any
    mechanism, anywhere in app/, that would let an operator or an
    automated process tell this row apart from one that's genuinely
    still in progress. As of this writing there is none -- this is the
    same defect legacy had, for this one specific failure mode.
    """
    with migrated_schema() as conn:
        run_id = collector_module._start_run(conn, trigger="scheduled")
        collector_module._touch_heartbeat(conn, run_id)
        # <-- a real SIGKILL happens here; no further Python code runs,
        # _finalize_run() is never reached, nothing rolls back (both
        # prior statements already committed by design).

        row = conn.execute("SELECT status, heartbeat_at FROM runs WHERE id = %s", (run_id,)).fetchone()
        assert row["status"] == "running"  # confirms the row is genuinely stuck, not auto-healed

        # Backdate the heartbeat to simulate real elapsed time (a fresh
        # heartbeat and a stale one must be distinguishable to whatever
        # mechanism is supposed to catch this).
        conn.execute(
            "UPDATE runs SET heartbeat_at = now() - interval '2 hours' WHERE id = %s",
            (run_id,),
        )
        conn.commit()

        stale_cutoff = timedelta(minutes=30)
        stale_row = conn.execute(
            "SELECT id FROM runs WHERE status = 'running' AND heartbeat_at < now() - (%s || ' seconds')::interval",
            (stale_cutoff.total_seconds(),),
        ).fetchone()
        # The QUERY above proves the DATA supports detection (idx_runs_status_running
        # + heartbeat_at are enough to write this). The assertion below is
        # about whether the APPLICATION actually does -- it doesn't: no
        # caller anywhere in app/ ever runs an equivalent query, so this
        # row will never actually be surfaced to anyone. Asserting
        # `stale_row is None` here would be testing the wrong thing (the
        # SQL clearly finds it); the real assertion is that no consumer
        # exists to act on it, which the reason string above documents
        # and this next check pins structurally.
        assert stale_row is not None  # the data CAN answer the question...

        import inspect

        from app.services import admin as admin_module

        health_runs_source = inspect.getsource(admin_module.health_runs)
        health_overview_source = inspect.getsource(admin_module.health_overview)
        # ...but ADMIN-API's own read path over `runs` -- the only place
        # in this codebase that reads the `runs` table back out for a
        # human to look at -- never mentions heartbeat_at at all, so a
        # stuck row like this one is reported with whatever `status` it
        # was last left at ('running'), forever, indistinguishable from
        # a run that started 30 seconds ago.
        assert "heartbeat_at" in health_runs_source or "heartbeat_at" in health_overview_source, (
            "expected the admin health/runs read path to compare heartbeat_at against now() "
            "somewhere so a stale 'running' row is surfaced as such, not reported as live"
        )
