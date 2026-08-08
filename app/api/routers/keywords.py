"""Admin: manage the bilingual (EN/BN) keyword terms across admin-defined
sections; "rematch today" re-runs phase 2 against already-fetched
articles. See frontend-contract.md §3.8 and §9 ("GET /keywords?section=",
"POST /keywords", "PATCH /keywords/{id}", "DELETE /keywords/{id}",
"POST /keywords/rematch").

Every route is gated by `require_admin` (app.api.deps) — same
enforcement note as app/api/routers/sources.py. `toggle` is an extra
convenience action beyond §9's proposed shape, mirroring sources.py's
identical addition and the mock's wired `k.toggle` action.

KEYWORDS-CONFIG additions: `PATCH /sections/{section}` bulk-renames a
section (there's no dedicated sections table — see
app.services.admin.list_keyword_sections); `GET /export.xlsx` /
`POST /import` are a full round-trip Excel export/upsert-import pair
(app.services.admin.export_keywords_xlsx /
.import_keywords_xlsx). `POST /import` reads the raw request body, not
`UploadFile` — see that route's own docstring for why.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from app.api.deps import AuthenticatedUser, require_admin
from app.api.schemas.admin import (
    KeywordCreate,
    KeywordImportResultOut,
    KeywordOut,
    KeywordSectionOut,
    KeywordSectionRename,
    KeywordUpdate,
    RematchResult,
)
from app.db.connection import get_connection
from app.services import admin as admin_service

router = APIRouter(prefix="/keywords", tags=["admin", "keywords"])

XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


@router.get("", response_model=list[KeywordOut])
def list_keywords(section: str | None = None, admin: AuthenticatedUser = Depends(require_admin)) -> list[KeywordOut]:
    with get_connection() as conn:
        rows = admin_service.list_keywords(conn, section=section)
    return [KeywordOut(**row) for row in rows]


@router.get("/sections", response_model=list[KeywordSectionOut])
def list_sections(admin: AuthenticatedUser = Depends(require_admin)) -> list[KeywordSectionOut]:
    with get_connection() as conn:
        rows = admin_service.list_keyword_sections(conn)
    return [KeywordSectionOut(**row) for row in rows]


@router.patch("/sections/{section}", response_model=list[KeywordSectionOut])
def rename_section(
    section: str, body: KeywordSectionRename, admin: AuthenticatedUser = Depends(require_admin)
) -> list[KeywordSectionOut]:
    """Bulk-renames every keyword currently in `section` to `body.name`
    — the "manage" half of task 1's custom-sections work (there is no
    dedicated sections table to rename a row in; see
    app.services.admin.rename_keyword_section). Returns the refreshed
    section list, same shape as `GET /keywords/sections`, so the
    frontend can redraw its section chips from one response.
    """
    with get_connection() as conn:
        admin_service.rename_keyword_section(conn, section, body.name)
        rows = admin_service.list_keyword_sections(conn)
    return [KeywordSectionOut(**row) for row in rows]


@router.get("/export.xlsx")
def export_keywords_xlsx(admin: AuthenticatedUser = Depends(require_admin)) -> Response:
    with get_connection() as conn:
        content = admin_service.export_keywords_xlsx(conn)
    return Response(
        content=content,
        media_type=XLSX_MEDIA_TYPE,
        headers={"Content-Disposition": 'attachment; filename="keywords.xlsx"'},
    )


@router.post("/import", response_model=KeywordImportResultOut)
async def import_keywords_xlsx(
    request: Request, admin: AuthenticatedUser = Depends(require_admin)
) -> KeywordImportResultOut:
    """Reads the uploaded `.xlsx` from the raw request body (not
    `UploadFile`/multipart form data — `python-multipart` isn't an
    installed dependency, and the mission flagged new dependencies back
    rather than adding them; the frontend instead PUTs the file's raw
    bytes with `fetch(..., {body: file})`, which is exactly what
    `await request.body()` reads here). See
    app.services.admin.import_keywords_xlsx for the upsert semantics and
    error handling.
    """
    content = await request.body()
    if not content:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="no file content in request body")
    with get_connection() as conn:
        result = admin_service.import_keywords_xlsx(conn, content)
    return KeywordImportResultOut(**result)


@router.post("", response_model=KeywordOut, status_code=status.HTTP_201_CREATED)
def create_keyword(body: KeywordCreate, admin: AuthenticatedUser = Depends(require_admin)) -> KeywordOut:
    with get_connection() as conn:
        row = admin_service.create_keyword(
            conn, section=body.section, term_en=body.term_en, term_bn=body.term_bn, enabled=body.enabled
        )
    return KeywordOut(**row)


@router.patch("/{keyword_id}", response_model=KeywordOut)
def update_keyword(
    keyword_id: int, body: KeywordUpdate, admin: AuthenticatedUser = Depends(require_admin)
) -> KeywordOut:
    with get_connection() as conn:
        row = admin_service.update_keyword(conn, keyword_id, **body.model_dump(exclude_unset=True))
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="keyword not found")
    return KeywordOut(**row)


@router.delete("/{keyword_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_keyword(keyword_id: int, admin: AuthenticatedUser = Depends(require_admin)) -> None:
    with get_connection() as conn:
        deleted = admin_service.delete_keyword(conn, keyword_id)
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="keyword not found")


@router.post("/{keyword_id}/toggle", response_model=KeywordOut)
def toggle_keyword(keyword_id: int, admin: AuthenticatedUser = Depends(require_admin)) -> KeywordOut:
    with get_connection() as conn:
        row = admin_service.toggle_keyword(conn, keyword_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="keyword not found")
    return KeywordOut(**row)


@router.post("/rematch", response_model=RematchResult)
def rematch(admin: AuthenticatedUser = Depends(require_admin)) -> RematchResult:
    with get_connection() as conn:
        result = admin_service.rematch_today(conn)
    return RematchResult(**result)
