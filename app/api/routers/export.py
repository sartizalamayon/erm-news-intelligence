"""Ad-hoc Excel/CSV export builder: story-set/date/section/column
(selection AND ordering)/sort selection, live row-count preview, named
presets. See frontend-contract.md §3.4 and frontend/js/screens/export.js's
header comment for the exact shape this was built against.

Every route requires an authenticated session (`get_current_user`) —
this is a user-facing screen, not an admin one.

Both `format=xlsx` and `format=csv` are real writers now
(app.services.export, backed by `openpyxl` — added to pyproject.toml by
ARCH-REPO, commit 9ba3ad9, after this router originally had to stub the
xlsx path at 501 for its absence). `columns` (comma-separated, order
preserved) is accepted on both `GET /export/preview` and `POST /export`
— FE-WIRE flagged that export.js's `params()` doesn't send the user's
column selection yet, so today every caller gets `DEFAULT_COLUMNS`
unless a future frontend change starts sending `columns` explicitly;
the API side of that wiring is complete either way.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status

from app.api.deps import AuthenticatedUser, get_current_user
from app.api.schemas.core import ExportPresetIn, ExportPresetOut, ExportPreviewOut
from app.db.connection import get_connection
from app.services import export as export_service

router = APIRouter(prefix="/export", tags=["export"])

XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _split_list(value: str) -> list[str]:
    return [s for s in value.split(",") if s] if value else []


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
    columns: str = "",
    sort: str = "relevance",
    format: str = "xlsx",
    user: AuthenticatedUser = Depends(get_current_user),
) -> ExportPreviewOut:
    _validate_filter(filter)
    # `columns` doesn't change the row count/summary a preview reports,
    # but is accepted (and validated by resolve_columns downstream, via
    # the writers) so the same query string works unmodified for both
    # GET /export/preview and POST /export.
    export_service.resolve_columns(_split_list(columns))
    with get_connection() as conn:
        result = export_service.preview(
            conn,
            filter=filter,
            top_n=topN,
            scope=scope,
            date_from=from_,
            date_to=to,
            sections=_split_list(sections),
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
    sections = body.sections if isinstance(body.sections, list) else _split_list(body.sections)
    columns = body.columns if isinstance(body.columns, list) else _split_list(body.columns)
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
            columns=columns,
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
    columns: str = "",
    sort: str = "relevance",
    format: str = "xlsx",
    user: AuthenticatedUser = Depends(get_current_user),
) -> Response:
    _validate_filter(filter)
    if format not in ("xlsx", "csv"):
        raise HTTPException(status_code=422, detail="format must be one of xlsx, csv")

    writer = export_service.export_xlsx if format == "xlsx" else export_service.export_csv
    media_type = XLSX_MEDIA_TYPE if format == "xlsx" else "text/csv"
    with get_connection() as conn:
        content, filename = writer(
            conn,
            filter=filter,
            top_n=topN,
            scope=scope,
            date_from=from_,
            date_to=to,
            sections=_split_list(sections),
            sort=sort,
            columns=_split_list(columns),
        )
    return Response(
        content=content,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
