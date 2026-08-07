"""Admin: manage the ~271 bilingual (EN/BN) keyword terms across 5
sections; "rematch today" re-runs phase 2 against already-fetched
articles. See frontend-contract.md §3.8 and §9 ("GET /keywords?section=",
"POST /keywords", "PATCH /keywords/{id}", "DELETE /keywords/{id}",
"POST /keywords/rematch").

Every route is gated by `require_admin` (app.api.deps) — same
enforcement note as app/api/routers/sources.py. `toggle` is an extra
convenience action beyond §9's proposed shape, mirroring sources.py's
identical addition and the mock's wired `k.toggle` action.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from app.api.deps import AuthenticatedUser, require_admin
from app.api.schemas.admin import (
    KeywordCreate,
    KeywordOut,
    KeywordSectionOut,
    KeywordUpdate,
    RematchResult,
)
from app.db.connection import get_connection
from app.services import admin as admin_service

router = APIRouter(prefix="/keywords", tags=["admin", "keywords"])


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
