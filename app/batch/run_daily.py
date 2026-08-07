"""Daily batch orchestration — the P1 fetch-layer entrypoint.

`main()` loads the full source registry (app/sources/registry.py),
runs the two-tier bounded fan-out (app/services/collector.py), and
writes results in one batched transaction when `DATABASE_URL` is
configured. When it isn't (local dev / CI default — see
tests/conftest.py's env defaults), this runs fetch-only: real discovery,
no DB write, so the entrypoint stays exercisable without a live
Postgres. `validate_production_settings` still fails fast if
`app_env=production` and `DATABASE_URL` is genuinely missing.

Per-source isolation is enforced one level down, inside
`app.services.collector.fetch_all`/`_run_one_source` — a single
source's `SourceError` (or, deliberately, any unexpected exception —
see that module's docstring) never aborts this entrypoint; it is
recorded as that source's own `failed` status and logged.

`run_collection(conn, ..., trigger="scheduled")` — this is the only
real entrypoint, wired to the daily GitHub Actions cron (ADR §1), so
every run through here is `'scheduled'`, not `'manual'` (the schema's
own default, meant for a future admin-triggered "run now" button this
module doesn't implement). Run-lifecycle bookkeeping itself
(`runs` row created/heartbeated/finalized) lives in
`app.services.collector.run_collection` — see that module's docstring
for why (RT-1, docs/red-team-findings.md).

**RT-1-residual (docs/red-team-findings.md; DEPLOY mission):** before
`run_collection` touches the `runs` table again, this entrypoint first
calls `app.db.reaper.reap_stale_runs(conn)` — marking any *previous*
run still stuck at `status='running'` with a heartbeat that stopped
advancing (a hard kill: SIGKILL, OOM, or `daily-batch.yml`'s own
`timeout-minutes`, none of which `run_collection`'s own exception
handling can observe) as `status='error'` before starting fresh. Every
new invocation of this entrypoint — scheduled or manual — self-heals any
abandoned row from a previous one, by construction. See
`app/db/reaper.py`'s module docstring for the full RT-1-residual
writeup and the stale-threshold reasoning.
"""

from __future__ import annotations

from app.config import get_settings, validate_production_settings
from app.db.connection import get_connection
from app.db.reaper import reap_stale_runs
from app.logging import configure_logging, get_logger
from app.services.collector import run_collection
from app.sources.registry import all_sources, load_all

logger = get_logger(__name__)


def main() -> int:
    configure_logging()
    settings = get_settings()
    validate_production_settings(settings)

    load_all()
    sources = all_sources()
    logger.info(
        "batch_run_start",
        extra={"app_env": settings.app_env, "registered_sources": len(sources)},
    )

    if not sources:
        logger.info("batch_run_complete", extra={"sources_run": 0})
        return 0

    if settings.database_url:
        with get_connection() as conn:
            reaped = reap_stale_runs(conn)
            if reaped:
                logger.warning(
                    "stale_runs_reaped_before_run",
                    extra={"run_ids": reaped, "count": len(reaped)},
                )
            result = run_collection(conn, source_classes=sources)
    else:
        logger.warning(
            "batch_run_no_database_configured",
            extra={"app_env": settings.app_env, "note": "running fetch-only, nothing will be written"},
        )
        result = run_collection(None, source_classes=sources)

    logger.info(
        "batch_run_complete",
        extra={
            "sources_run": len(result.sources),
            "sources_ok": sum(1 for s in result.sources if s.status == "ok"),
            "sources_empty": result.total_empty_sources,
            "sources_failed": result.total_failed_sources,
            "total_fetched": result.total_fetched,
            "total_inserted": result.total_inserted,
            "total_duplicate": result.total_duplicate,
        },
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
