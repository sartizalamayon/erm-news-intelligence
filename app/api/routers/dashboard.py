"""Admin: the storage dashboard — current usage, ceiling, % used,
projected days remaining, manual purge with dry-run + confirmation, and
the export -> purge -> re-import data-lifecycle journey (ADR §9.2, DoD
#5/#6 of the ADMIN-API mission).

NOT the general app Dashboard screen (frontend-contract.md §3.1-3.2,
short-term/long-term today-and-trends views) — this original stub's own
docstring named that a separate, larger, not-yet-built surface ("the
largest net-new block in the project, likely its own agent"), and the
ADMIN-API mission's context-reading list names only §3.7/§3.8/§3.9/§3.10
— §3.1/§3.2 are out of scope here. Everything below is scoped under
`/dashboard/storage/*` specifically so it can never collide with
whoever builds `/dashboard/today` or `/dashboard/trends` next; this
router's existing bare `/dashboard` prefix (from the original stub) is
left otherwise untouched for that future work.

Backed by `app.services.storage` (ARCH-DB's accounting + purge —
consumed here, never reimplemented; ARCH-DB is concurrently fixing a
preview/delete race in that module, so this router calls its public
functions and inherits that fix automatically once landed) and
`app.services.data_transfer` (export/import).

Every route is gated by `require_admin` (app.api.deps).
"""

from __future__ import annotations

import dataclasses
from datetime import date

from fastapi import APIRouter, Depends

from app.api.deps import AuthenticatedUser, require_admin
from app.api.schemas.admin import (
    ExportRequestBody,
    ExportResultOut,
    ImportRequestBody,
    ImportResultOut,
    PurgeRequestBody,
    PurgeResultOut,
    StorageUsageOut,
)
from app.db.connection import get_connection
from app.services import admin as admin_service

router = APIRouter(prefix="/dashboard", tags=["dashboard", "admin", "storage"])


@router.get("/storage", response_model=StorageUsageOut)
def get_storage(admin: AuthenticatedUser = Depends(require_admin)) -> StorageUsageOut:
    with get_connection() as conn:
        usage = admin_service.storage_usage(conn)
    return StorageUsageOut(**dataclasses.asdict(usage))


@router.post("/storage/purge", response_model=PurgeResultOut)
def purge(body: PurgeRequestBody, admin: AuthenticatedUser = Depends(require_admin)) -> PurgeResultOut:
    older_than = date.fromisoformat(body.older_than) if body.older_than else None
    with get_connection() as conn:
        result = admin_service.run_purge(
            conn,
            operation=body.operation,
            older_than_days=body.older_than_days,
            older_than=older_than,
            require_exported=body.require_exported,
            confirm=body.confirm,
        )
    return PurgeResultOut(**dataclasses.asdict(result))


@router.post("/storage/export", response_model=ExportResultOut)
def export(body: ExportRequestBody, admin: AuthenticatedUser = Depends(require_admin)) -> ExportResultOut:
    day_from = date.fromisoformat(body.day_from) if body.day_from else None
    day_to = date.fromisoformat(body.day_to) if body.day_to else None
    with get_connection() as conn:
        manifest = admin_service.run_export(
            conn,
            day_from=day_from,
            day_to=day_to,
            review_status=body.review_status,
            mark_exported=body.mark_exported,
            path=body.path,
        )
    return ExportResultOut(
        path=manifest.path,
        articles_exported=manifest.articles_exported,
        bodies_exported=manifest.bodies_exported,
        exported_at=manifest.exported_at.isoformat(),
    )


@router.post("/storage/import", response_model=ImportResultOut)
def import_(body: ImportRequestBody, admin: AuthenticatedUser = Depends(require_admin)) -> ImportResultOut:
    with get_connection() as conn:
        report = admin_service.run_import(conn, path=body.path)
    return ImportResultOut(**dataclasses.asdict(report))
