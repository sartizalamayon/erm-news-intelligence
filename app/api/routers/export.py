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

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse

from app.api.deps import AuthenticatedUser, get_current_user
from app.api.schemas.core import ExportDownloadBody, ExportPresetIn, ExportPresetOut, ExportPreviewOut
from app.db.connection import get_connection
from app.services import export as export_service

router = APIRouter(prefix="/export", tags=["export"])

XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _split_list(value: str) -> list[str]:
    return [s for s in value.split(",") if s] if value else []


def _as_list(value: str | list[str]) -> list[str]:
    return value if isinstance(value, list) else _split_list(value)


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
    sections = _as_list(body.sections)
    columns = _as_list(body.columns)
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
    body: ExportDownloadBody | None = None,
    user: AuthenticatedUser = Depends(get_current_user),
) -> StreamingResponse:
    # frontend/js/screens/export.js sends every param via the query
    # string with no JSON body at all (body stays None, every eff_*
    # below falls straight through to its query default — unchanged
    # behavior). A JSON-body caller's non-None fields win per-field over
    # the query string, so `format`/`filter`/etc. are never silently
    # dropped just because they arrived in the body instead of the URL.
    eff_filter = body.filter if body and body.filter is not None else filter
    eff_top_n = body.topN if body and body.topN is not None else topN
    eff_scope = body.scope if body and body.scope is not None else scope
    eff_from = body.from_ if body and body.from_ is not None else from_
    eff_to = body.to if body and body.to is not None else to
    eff_sections = body.sections if body and body.sections is not None else sections
    eff_columns = body.columns if body and body.columns is not None else columns
    eff_sort = body.sort if body and body.sort is not None else sort
    eff_format = body.format if body and body.format is not None else format

    _validate_filter(eff_filter)
    if eff_format not in ("xlsx", "csv"):
        raise HTTPException(status_code=422, detail="format must be one of xlsx, csv")

    # STREAMED, not buffered. app.services.export.iter_export_rows
    # carries the measurements: buffering peaked at 270 MB for a single
    # CSV of one year of the dev corpus, on a Render FREE instance with
    # 512 MB total and a ~53 MB baseline. Two concurrent downloads, or
    # one alongside the nightly batch, was an OOM.
    #
    # No `with get_connection()` here on purpose, and it is not an
    # oversight. The writers below read through a server-side cursor and
    # therefore need the connection to stay open for as long as the
    # response body is being produced -- long after this function has
    # returned. A block opened here would close it the moment the
    # generator was handed to Starlette. Each writer opens and owns its
    # own connection instead; see stream_csv's docstring, including what
    # that means for a client that disconnects mid-download.
    #
    # `_filename` is a pure function of the request parameters, so the
    # Content-Disposition header is known before a single row is read --
    # which is what makes the header/body split work at all.
    streamer = export_service.stream_xlsx if eff_format == "xlsx" else export_service.stream_csv
    media_type = XLSX_MEDIA_TYPE if eff_format == "xlsx" else "text/csv"
    filename = export_service.filename_for(eff_filter, eff_top_n, eff_scope, eff_format)
    return StreamingResponse(
        streamer(
            get_connection,
            filter=eff_filter,
            top_n=eff_top_n,
            scope=eff_scope,
            date_from=eff_from,
            date_to=eff_to,
            sections=_as_list(eff_sections),
            sort=eff_sort,
            columns=_as_list(eff_columns),
        ),
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
