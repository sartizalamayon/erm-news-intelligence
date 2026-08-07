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

Every route is gated by `require_admin` (app.api.deps).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from app.api.deps import AuthenticatedUser, require_admin
from app.api.schemas.admin import (
    HealthOverviewOut,
    HealthSourceRowOut,
    RunRowOut,
    SourceDiagnoseResult,
    SourceTestResult,
    StageRunResult,
)
from app.db.connection import get_connection
from app.services import admin as admin_service

router = APIRouter(prefix="/health", tags=["admin", "health"])


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
