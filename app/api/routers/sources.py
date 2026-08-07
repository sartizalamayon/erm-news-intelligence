"""Admin: manage the ~46 configured news outlets (fetch method(s),
language, enabled/disabled, test/edit/delete). See frontend-contract.md
§3.7 and §9 ("GET /sources", "POST /sources", "PATCH /sources/{id}",
"DELETE /sources/{id}", "POST /sources/{id}/test").

Not to be confused with app/sources/ (the fetch-plugin code layer) —
this router is the admin CRUD API over source *configuration rows*
(the `sources` table, migrations/0002_reference_tables.sql), a distinct
concern from the plugin classes that do the fetching. `test` below
DOES reach into that plugin layer (read-only — app.sources.registry),
via app.services.admin.probe_source, for a real synchronous-feeling
probe rather than a cached status read.

Every route is gated by `require_admin` (app.api.deps) — see that
module's docstring for why route-layer enforcement is the only real
control (the frontend only hides admin nav, never blocks navigation).
`toggle` is an extra convenience action beyond frontend-contract.md
§9's proposed shape (a proposal, not an observed fact) — `s.toggle` is
a wired write action in the mock with no documented payload, and a
single-purpose endpoint is simpler for a caller than a full PATCH for
"just flip enabled".
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from app.api.deps import AuthenticatedUser, require_admin
from app.api.schemas.admin import (
    SourceCreate,
    SourceDiagnoseResult,
    SourceOut,
    SourceTestResult,
    SourceUpdate,
)
from app.db.connection import get_connection
from app.services import admin as admin_service

router = APIRouter(prefix="/sources", tags=["admin", "sources"])


@router.get("", response_model=list[SourceOut])
def list_sources(enabled: bool | None = None, admin: AuthenticatedUser = Depends(require_admin)) -> list[SourceOut]:
    with get_connection() as conn:
        rows = admin_service.list_sources(conn, enabled=enabled)
    return [SourceOut(**row) for row in rows]


@router.post("", response_model=SourceOut, status_code=status.HTTP_201_CREATED)
def create_source(body: SourceCreate, admin: AuthenticatedUser = Depends(require_admin)) -> SourceOut:
    with get_connection() as conn:
        row = admin_service.create_source(
            conn, name=body.name, methods=body.methods, language=body.language, enabled=body.enabled, config=body.config
        )
    return SourceOut(**row)


@router.patch("/{source_id}", response_model=SourceOut)
def update_source(source_id: int, body: SourceUpdate, admin: AuthenticatedUser = Depends(require_admin)) -> SourceOut:
    with get_connection() as conn:
        row = admin_service.update_source(conn, source_id, **body.model_dump(exclude_unset=True))
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="source not found")
    return SourceOut(**row)


@router.delete("/{source_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_source(source_id: int, admin: AuthenticatedUser = Depends(require_admin)) -> None:
    with get_connection() as conn:
        deleted = admin_service.delete_source(conn, source_id)
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="source not found")


@router.post("/{source_id}/toggle", response_model=SourceOut)
def toggle_source(source_id: int, admin: AuthenticatedUser = Depends(require_admin)) -> SourceOut:
    with get_connection() as conn:
        row = admin_service.toggle_source(conn, source_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="source not found")
    return SourceOut(**row)


@router.post("/{source_id}/test", response_model=SourceTestResult)
def test_source(source_id: int, admin: AuthenticatedUser = Depends(require_admin)) -> SourceTestResult:
    with get_connection() as conn:
        result = admin_service.probe_source(conn, source_id)
    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="source not found")
    return SourceTestResult(**result)


@router.post("/{source_id}/diagnose", response_model=SourceDiagnoseResult)
def diagnose_source(source_id: int, admin: AuthenticatedUser = Depends(require_admin)) -> SourceDiagnoseResult:
    with get_connection() as conn:
        result = admin_service.diagnose_source(conn, source_id)
    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="source not found")
    return SourceDiagnoseResult(**result)
