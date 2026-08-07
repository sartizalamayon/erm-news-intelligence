"""Ad-hoc Excel/CSV export builder: story-set/date/section/sort
selection, live row-count preview, named presets. See
frontend-contract.md §3.4 and frontend/js/screens/export.js's header
comment for the exact shape this was built against.

Every route requires an authenticated session (`get_current_user`) —
this is a user-facing screen, not an admin one.

xlsx needs `openpyxl`, which is NOT installed (pyproject.toml is
ARCH-REPO's file to edit, not this task's) — `POST /export` with
`format=xlsx` returns 501 with that flagged explicitly rather than
silently returning something that isn't really an xlsx file. `format=csv`
is fully implemented via the stdlib `csv` module (app.services.export).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status

from app.api.deps import AuthenticatedUser, get_current_user
from app.api.schemas.core import ExportPresetIn, ExportPresetOut, ExportPreviewOut
from app.db.connection import get_connection
from app.services import export as export_service

router = APIRouter(prefix="/export", tags=["export"])


def _split_sections(sections: str) -> list[str]:
    return [s for s in sections.split(",") if s] if sections else []


def _validate_filter(filter: str) -> None:
    if filter not in export_service.FILTER_KEYS:
        raise HTTPException(status_code=422, detail=f"filter must be one of {export_service.FILTER_KEYS}")


@router.get("/preview", response_model=ExportPreviewOut)
def export_preview(
    filter: str = "topn",
    topN: int = Query(25),
    scope: str = "today",
    from_: str | None = Query(None, alias="from"),
    to: str | None = None,
    sections: str = "",
    sort: str = "relevance",
    format: str = "xlsx",
    user: AuthenticatedUser = Depends(get_current_user),
) -> ExportPreviewOut:
    _validate_filter(filter)
    with get_connection() as conn:
        result = export_service.preview(
            conn,
            filter=filter,
            top_n=topN,
            scope=scope,
            date_from=from_,
            date_to=to,
            sections=_split_sections(sections),
            sort=sort,
            format=format,
        )
    return ExportPreviewOut(**result)


@router.get("/presets", response_model=list[ExportPresetOut])
def list_presets(user: AuthenticatedUser = Depends(get_current_user)) -> list[ExportPresetOut]:
    with get_connection() as conn:
        rows = export_service.list_presets(conn)
    return [ExportPresetOut(**row) for row in rows]


@router.post("/presets", response_model=ExportPresetOut, status_code=status.HTTP_201_CREATED)
def create_preset(body: ExportPresetIn, user: AuthenticatedUser = Depends(get_current_user)) -> ExportPresetOut:
    _validate_filter(body.filter)
    sections = body.sections if isinstance(body.sections, list) else _split_sections(body.sections)
    with get_connection() as conn:
        row = export_service.create_preset(
            conn,
            name=body.name,
            note=body.note,
            filter=body.filter,
            topN=body.topN,
            scope=body.scope,
            sections=sections,
            sort=body.sort,
            format=body.format,
        )
    return ExportPresetOut(**row)


@router.post("")
def do_export(
    filter: str = "topn",
    topN: int = Query(25),
    scope: str = "today",
    from_: str | None = Query(None, alias="from"),
    to: str | None = None,
    sections: str = "",
    sort: str = "relevance",
    format: str = "xlsx",
    user: AuthenticatedUser = Depends(get_current_user),
) -> Response:
    _validate_filter(filter)
    if format == "xlsx":
        raise HTTPException(
            status_code=501,
            detail=(
                "xlsx export requires the 'openpyxl' package, which is not an installed dependency "
                "(pyproject.toml is ARCH-REPO's file — flagged, not added here). Use format=csv for now."
            ),
        )
    if format != "csv":
        raise HTTPException(status_code=422, detail="format must be one of xlsx, csv")
    with get_connection() as conn:
        content, filename = export_service.export_csv(
            conn,
            filter=filter,
            top_n=topN,
            scope=scope,
            date_from=from_,
            date_to=to,
            sections=_split_sections(sections),
            sort=sort,
        )
    return Response(
        content=content,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
