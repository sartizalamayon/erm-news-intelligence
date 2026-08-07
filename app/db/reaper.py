"""Stale-run reaper — closes RT-1-residual (docs/red-team-findings.md;
see tests/test_redteam_orphaned_runs.py's
`test_a_run_that_stops_advancing_its_heartbeat_is_detected_as_stale`,
which this module exists to answer).

Recap of the gap: `app.services.collector.run_collection` (RT-1's
original fix, P1-INGEST) inserts a `runs` row via `_start_run`, commits
immediately, touches `heartbeat_at` via `_touch_heartbeat` once per
completed source (also committed immediately), and finalizes the row
`status='done'`/`'error'` via `_finalize_run` on any Python-observable
exit path (`_finalize_run` runs in both the success path and the
`except Exception` handler). That closes every failure mode Python's
own exception handling can see. It does NOT close a genuine hard kill —
SIGKILL, the OOM killer, or (the realistic case for this project)
GitHub Actions' own `timeout-minutes` on `.github/workflows/daily-batch.yml`
— landing between two of those commits: `finally`/`except` blocks don't
run under SIGKILL, so the row is left at `status='running'` with a
`heartbeat_at` that will never advance again. `idx_runs_status_running`
and `heartbeat_at` (migrations/0002_reference_tables.sql) are exactly
the columns a watchdog needs to tell that apart from a run that is
genuinely still in progress — but as of RT-1-residual, nothing in app/
ever read `heartbeat_at` back out to act on it. Legacy's own un-reaped
equivalent left 16 of 110 runs (14.5%) stuck this way forever.

Why this lives in app/db/reaper.py rather than alongside `_start_run` /
`_touch_heartbeat` / `_finalize_run` in app/services/collector.py (which
is where README-rebuild.md's own "recommended, not implemented here"
note — written by ARCH-REPO, before this task existed — suggested
putting it): app/services/** is P1-INGEST's owned surface for this task,
not this task's. app/db/** is otherwise ARCH-DB's territory too, but
this one new file is this task's explicit, sole exception to that
boundary. The result is the same shape either way: one small, focused
function, called at the very start of every batch run — see
app/batch/run_daily.py, which is this task's file and calls
`reap_stale_runs` before `run_collection` touches the `runs` table
again, so every new run self-heals any previous run's abandoned row
before starting its own. That is "prevents", the property RT-1's own
mission brief asked for (see the red-team test's own docstring), not
just "reaps after the fact on some separate schedule" — there is no
separate reaper workflow/schedule to keep in sync with the batch cron,
by construction.

Threshold: `stale_after` defaults to 30 minutes. `run_collection`
touches the heartbeat once per completed source (`app/services/collector.py`),
so the real gap between heartbeats during a live, healthy run is however
long the slowest single source's fetch takes — seconds to low minutes
per the measured ~2.5-minute full 45-source collection phase
(README-rebuild.md's "Timeout: 150 minutes" section) — 30 minutes is
comfortably above that with a lot of headroom before a live run risks
being reaped out from under itself, while still being well under
daily-batch.yml's own 150-minute job timeout, so a run genuinely killed
by that timeout is caught by the very next invocation rather than
sitting stale for up to a day. Callers needing a different value (e.g. a
future admin-configurable setting) pass `stale_after` explicitly; no
env var is read here directly, consistent with app.config.Settings being
the only place this codebase reads process config from (see that
module's own docstring) — promoting this to a Settings field is a
natural follow-up, flagged rather than done here since app/config.py is
contested by concurrent work this session (see this task's report).
"""

from __future__ import annotations

from datetime import timedelta

from psycopg.types.json import Jsonb

from app.db.connection import DictConnection
from app.logging import get_logger

logger = get_logger(__name__)

DEFAULT_STALE_AFTER = timedelta(minutes=30)


def reap_stale_runs(conn: DictConnection, *, stale_after: timedelta = DEFAULT_STALE_AFTER) -> list[int]:
    """Mark every `runs` row still `status='running'` whose heartbeat has
    gone stale as `status='error'`, `finished_at=now()`.

    "Stale" = `COALESCE(heartbeat_at, started_at)` older than
    `stale_after`. The COALESCE matters: `heartbeat_at` is NULL until
    the first `_touch_heartbeat` call (`_start_run` only sets
    `started_at`), so a run killed before completing even its first
    source — plausible for a source-registry bug or an immediate crash —
    would otherwise have a NULL `heartbeat_at` that no naive
    `heartbeat_at < now() - interval` comparison catches; falling back to
    `started_at` closes that gap the same way.

    Uses `idx_runs_status_running` (migrations/0002_reference_tables.sql)
    — `WHERE status = 'running'` is exactly the query that partial index's
    own header comment names as its intended use.

    Committed immediately inside this function, matching
    `_start_run`/`_touch_heartbeat`/`_finalize_run`'s own reasoning
    (app/services/collector.py): a reap that stays uncommitted until some
    later, larger transaction closes is exactly the kind of fragility RT-1
    already fixed once for run bookkeeping — this function shouldn't
    reintroduce it for the reaper itself.

    Returns the ids of every row reaped (empty list, the common case, on
    a healthy day with nothing stale to find).
    """
    rows = conn.execute(
        """
        UPDATE runs
        SET status = 'error',
            finished_at = now(),
            details = details || %s::jsonb
        WHERE status = 'running'
          AND COALESCE(heartbeat_at, started_at) < now() - %s
        RETURNING id
        """,
        (
            Jsonb(
                {
                    "reaped_by": "app.db.reaper.reap_stale_runs",
                    "reason": "heartbeat_at (or started_at) exceeded the stale threshold -- "
                    "consistent with a hard process kill (SIGKILL/OOM/Actions timeout) that "
                    "never reached _finalize_run",
                }
            ),
            stale_after,
        ),
    ).fetchall()
    conn.commit()

    reaped_ids = [int(row["id"]) for row in rows]
    if reaped_ids:
        logger.warning(
            "stale_runs_reaped",
            extra={"run_ids": reaped_ids, "count": len(reaped_ids), "stale_after_seconds": stale_after.total_seconds()},
        )
    return reaped_ids
