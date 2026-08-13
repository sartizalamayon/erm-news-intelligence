"""Admin: source-health operational dashboard — healthy/attention tiles,
the 5 pipeline stages with run/re-run controls, per-source status table,
run history. See frontend-contract.md §3.10 and §9 ("GET /health/overview",
"GET /health/sources", "GET /health/runs", "POST /pipeline/stages/{key}/run",
"POST /pipeline/run-all", "POST /health/sources/{id}/test",
"POST /health/sources/{id}/diagnose").

Deviation from §9's exact proposed paths, and why: `POST /pipeline/...`
in that proposal is unprefixed, but this router's prefix (`/health`) is
fixed by the original ARCH-REPO stub and this task's owned-files list
has no separate top-level router to host an unprefixed `/pipeline/*`
path without editing `app/api/routers/__init__.py` (not in this task's
owned-files list). Both pipeline-run actions are therefore mounted at
`/health/pipeline/stages/{key}/run` and `/health/pipeline/run-all`
instead — §9 is explicitly "a design proposal... not an observed fact",
so adapting it to fit an already-fixed router prefix is a reasonable,
documented deviation rather than an invented endpoint.

The KPI tiles frontend-contract.md §3.10 flags as hardcoded literals in
the mock ("the only screen... top-of-page KPIs aren't wired at all")
are computed for real here (app.services.admin.health_overview), from
the same `healthRows`/`runRows` data the table below shows — never a
fixed display value.

Pipeline stage runs dispatch to `app.services.enrichment.run_llm_enrichment`
(P3-LLM's already-built stage orchestrator, for translate/dedup/judge/
topic) or a minimal, bounded body-fetch built directly here against the
read-only `app.sources` plugin interface (for "Get article text", which
has no existing orchestrator anywhere in this codebase — see
app.services.admin's PIPELINE_STAGES docstring). Runs are synchronous
(no background job queue — none is a declared dependency, and this task
may not add one); each stage-run request blocks until that stage
finishes, which is a real limitation for the judge/topic stages against
a large batch. Flagged in this task's report, not solved here.

BUGFIX (the "Run all stages" bug report): those 5 stages above are
enrichment ONLY — they never fetch anything new. Before this fix, there
was ZERO HTTP endpoint anywhere in this codebase that called
`app.services.collector.run_collection` (the real P1 fetch layer); the
only caller was `app.batch.run_daily.main`, itself only ever invoked by
the daily GitHub Actions cron. `POST /health/pipeline/fetch/run` /
`GET /health/pipeline/fetch/status` below close that gap: a real,
admin-triggered "fetch today's news now", backgrounded (a full run
spans ~45 sources over a few minutes — see
`app.services.admin`'s "Fetch (P1) trigger" module section for the full
threading/progress-tracking design) so the request returns immediately
and progress is genuinely pollable while it runs, not a fake instant
"done". `POST /health/pipeline/match/run` runs P2 (keyword matching,
`app.services.matching` via `app.services.admin.run_match_phase` —
already existed as `rematch_today`/`POST /keywords/rematch`, exposed
here too so it's selectable as its own phase alongside fetch and the 5
enrichment stages) — confirmed genuinely separate from P1 by reading
`run_collection`'s own docstring, which is explicit that it never
computes `sections`/`keywords`/`review_status`.

BUGFIX-NAV (the "Run collection abandons match/enrich on navigation" bug
report): the three endpoints above still exist unchanged, but the
frontend no longer drives fetch -> match -> enrich sequencing by
awaiting each of them in turn from the browser — that was the actual
bug (frontend/js/screens/dashboard.js's old runCollection() checked a
`destroy()`-flipped `alive` flag between phases; navigating away mid-
fetch silently skipped match/enrich even though fetch itself kept
running fine server-side). `POST /health/pipeline/run` /
`GET /health/pipeline/run/status` below replace that client-side
conductor with one server-side background thread
(`app.services.admin.start_run`/`_run_pipeline_in_background`) that runs
whichever of fetch/match/enrich were requested, in order, to completion,
regardless of whether any client is still connected or polling. The
frontend now only starts the run once and polls the status endpoint to
RENDER progress — see `app.services.admin`'s "Consolidated run" module
section for the full design.

Every route is gated by `require_admin` (app.api.deps) — EXCEPT
`GET /health/ready` below, added by the DEPLOY mission (Render deploy
config, docs/deployment.md): a readiness probe Render's own health
checker calls has no way to authenticate (no session cookie, no
browser), so it deliberately sits outside `require_admin`, unlike every
other route in this file. Purely additive — nothing above this route was
touched.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from app.api.deps import AuthenticatedUser, require_admin
from app.api.schemas.admin import (
    FetchRunStatusOut,
    HealthOverviewOut,
    HealthSourceRowOut,
    RematchResult,
    ResetDayBody,
    ResetDayResult,
    RunRowOut,
    RunStartBody,
    RunStatusOut,
    SourceDiagnoseResult,
    SourceTestResult,
    StageRunResult,
)
from app.config import Settings, get_settings
from app.db.connection import get_connection
from app.logging import get_logger
from app.services import admin as admin_service

logger = get_logger(__name__)

router = APIRouter(prefix="/health", tags=["admin", "health"])


@router.get("/ready")
def readiness(settings: Settings = Depends(get_settings)) -> dict[str, str]:
    """Readiness probe for Render's `healthCheckPath` (render.yaml,
    docs/deployment.md) — distinct from `app.main`'s existing `GET
    /healthz`, which is a deliberate zero-dependency liveness probe only
    (see that route's own docstring: "keep those separate if a readiness
    probe is added later"). This is that later: added here, in
    app/api/routers/health.py, rather than in app/main.py, because
    app/main.py and app/middleware/** are out of this task's owned-files
    list this round.

    Runs a real `SELECT 1` against the configured database — a stale
    `/healthz` that only ever returns `{"status": "ok"}` cannot tell
    Render "the process is up but Supabase is unreachable" apart from
    "everything is fine", which is exactly the false-positive this route
    exists to close. `DATABASE_URL` unset (local dev / CI default) is
    treated as a legitimate non-production state, not a failure — there
    is nothing to check reachability of.

    `settings` is a FastAPI dependency (not the raw `get_settings()`
    singleton call every other route in this file uses) specifically so
    tests can override it per-request via `app.dependency_overrides`
    (see tests/test_deploy_readiness.py) without mutating the
    process-wide `lru_cache`d settings singleton other tests also share.
    """
    if not settings.database_url:
        return {"status": "ok", "database": "not_configured"}
    try:
        with get_connection(settings.database_url) as conn:
            conn.execute("SELECT 1")
    except Exception as exc:
        # Deliberately broad: ANY DB failure means "not ready", not just
        # the ones anticipated up front -- same reasoning
        # app.services.collector.py documents for its own broad catch.
        logger.error("readiness_db_unreachable", extra={"error": str(exc)})
        raise HTTPException(status_code=503, detail="database unreachable") from exc
    return {"status": "ok", "database": "reachable"}


@router.get("/overview", response_model=HealthOverviewOut)
def overview(admin: AuthenticatedUser = Depends(require_admin)) -> HealthOverviewOut:
    with get_connection() as conn:
        data = admin_service.health_overview(conn)
    return HealthOverviewOut(**data)


@router.get("/sources", response_model=list[HealthSourceRowOut])
def sources(admin: AuthenticatedUser = Depends(require_admin)) -> list[HealthSourceRowOut]:
    with get_connection() as conn:
        rows = admin_service.health_sources(conn)
    return [HealthSourceRowOut(**row) for row in rows]


@router.get("/runs", response_model=list[RunRowOut])
def runs(limit: int = 5, admin: AuthenticatedUser = Depends(require_admin)) -> list[RunRowOut]:
    with get_connection() as conn:
        rows = admin_service.health_runs(conn, limit=limit)
    return [RunRowOut(**row) for row in rows]


@router.post("/sources/{source_id}/test", response_model=SourceTestResult)
def test_source(source_id: int, admin: AuthenticatedUser = Depends(require_admin)) -> SourceTestResult:
    with get_connection() as conn:
        result = admin_service.probe_source(conn, source_id)
    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="source not found")
    return SourceTestResult(**result)


@router.post("/sources/{source_id}/diagnose", response_model=SourceDiagnoseResult)
def diagnose_source(source_id: int, admin: AuthenticatedUser = Depends(require_admin)) -> SourceDiagnoseResult:
    with get_connection() as conn:
        result = admin_service.diagnose_source(conn, source_id)
    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="source not found")
    return SourceDiagnoseResult(**result)


@router.post("/pipeline/stages/{stage_key}/run", response_model=StageRunResult)
def run_stage(stage_key: str, admin: AuthenticatedUser = Depends(require_admin)) -> StageRunResult:
    with get_connection() as conn:
        result = admin_service.run_pipeline_stage(conn, stage_key)
    return StageRunResult(**result)


@router.post("/pipeline/run-all", response_model=list[StageRunResult])
def run_all(admin: AuthenticatedUser = Depends(require_admin)) -> list[StageRunResult]:
    with get_connection() as conn:
        results = admin_service.run_pipeline_all(conn)
    return [StageRunResult(**r) for r in results]


@router.post("/pipeline/fetch/run", response_model=FetchRunStatusOut)
def run_fetch(admin: AuthenticatedUser = Depends(require_admin)) -> FetchRunStatusOut:
    """Starts a real P1 collection run for today (`trigger="manual"`) in
    a background thread and returns immediately — see
    `app.services.admin.start_fetch_run`'s docstring. Never blocks for
    the full multi-minute fetch duration; poll `GET
    /pipeline/fetch/status` for live progress. Safe to call while a
    fetch is already running — returns that run's current snapshot
    rather than starting a redundant second one.
    """
    return FetchRunStatusOut(**admin_service.start_fetch_run(trigger="manual"))


@router.get("/pipeline/fetch/status", response_model=FetchRunStatusOut)
def fetch_status(admin: AuthenticatedUser = Depends(require_admin)) -> FetchRunStatusOut:
    """Live snapshot of the most recent manual fetch trigger (in-process
    state, not a DB query — see `app.services.admin.fetch_status`'s
    docstring for why). `status="idle"` with an empty `sources` list if
    no fetch has run yet in this process.
    """
    return FetchRunStatusOut(**admin_service.fetch_status())


@router.post("/pipeline/match/run", response_model=RematchResult)
def run_match(admin: AuthenticatedUser = Depends(require_admin)) -> RematchResult:
    """Runs P2 (keyword matching) against today's already-fetched,
    not-yet-decided articles — `app.services.admin.run_match_phase`,
    which is `rematch_today` under a phase-consistent name (also reachable
    as `POST /keywords/rematch`; this is the same operation, exposed here
    too so it is selectable as its own phase alongside fetch and the 5
    enrichment stages on the dashboard's "Run collection" panel).
    Synchronous — matching is a fast, local regex pass with no network
    call, unlike fetch.
    """
    with get_connection() as conn:
        result = admin_service.run_match_phase(conn)
    return RematchResult(**result)


@router.post("/pipeline/run", response_model=RunStatusOut)
def start_run(body: RunStartBody, admin: AuthenticatedUser = Depends(require_admin)) -> RunStatusOut:
    """BUGFIX-NAV: the single entry point "Run collection" now uses.
    Starts fetch/match/enrich (whichever booleans are `true`) in ONE
    background thread, server-side, in that order — see
    `app.services.admin.start_run`'s docstring for the navigation-abandon
    bug this closes. Returns immediately with a "running" snapshot; poll
    `GET /pipeline/run/status` to render progress. The run itself
    proceeds to completion regardless of whether anything ever polls it
    again. Safe to call while a run is already in progress — returns
    that run's current snapshot rather than starting a redundant second
    one.
    """
    data = admin_service.start_run(fetch=body.fetch, match=body.match, enrich=body.enrich, trigger="manual")
    return RunStatusOut(**data)


@router.post("/pipeline/reset-day", response_model=ResetDayResult)
def reset_day(body: ResetDayBody, admin: AuthenticatedUser = Depends(require_admin)) -> ResetDayResult:
    """Make a day collectable again — delete its collected articles AND
    drop the dedup tombstones those deletions create.

    This exists because doing it by hand is a trap that already bit us
    in production-like use. `seen_urls` remembers every URL whose
    `articles` row stops existing (migrations/0016), so a plain
    `DELETE FROM articles` leaves the day empty but every URL still
    "seen" — the next collection then reports "0 new" for sources that
    are working perfectly, and looks for all the world like the
    scrapers broke. Measured live on 2026-08-14: 8,931 orphaned
    tombstones, sources showing "178 fetched · 4 new".

    Defaults to keeping approved/rejected rows — re-collecting is a
    data-collection operation and must not destroy review work.
    """
    with get_connection() as conn:
        result = admin_service.reset_day(conn, day=body.day, keep_reviewed=body.keep_reviewed)
    logger.info(
        "pipeline_day_reset",
        extra={"day": result["day"], "deleted": result["deleted"], "forgotten_urls": result["forgotten_urls"]},
    )
    return ResetDayResult(**result)


@router.get("/pipeline/run/status", response_model=RunStatusOut)
def run_status(admin: AuthenticatedUser = Depends(require_admin)) -> RunStatusOut:
    """Live snapshot of the most recent consolidated run this process has
    started — purely observational (see `app.services.admin.run_status`'s
    docstring). `status="idle"` with every phase `null` if no run has
    ever started yet in this process.
    """
    return RunStatusOut(**admin_service.run_status())
