"""Full-text search archive across every collected article (headline,
English translation, body), regardless of date/status. See
frontend-contract.md §3.6 and frontend/js/screens/search.js's header
comment for the exact shape this was built against.

Requires an authenticated session (`get_current_user`) — user-facing,
not admin-only.

Filters (date range, source, language, review status) are combinable,
mirroring app/api/routers/review.py's filter shape for consistency —
see app/services/search.py's module docstring for the perf diagnosis/
fix and the `status` vocabulary (including the explicit "never_matched"
state the owner asked for).
"""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends

from app.api.deps import AuthenticatedUser, get_current_user
from app.api.schemas.core import SearchOut
from app.db.connection import get_connection
from app.services import search as search_service

router = APIRouter(prefix="/search", tags=["search"])


@router.get("", response_model=SearchOut)
def search(
    q: str = "",
    page: int = 1,
    date_from: date | None = None,
    date_to: date | None = None,
    lang: str | None = None,
    status: str | None = None,
    exclude_source: str | None = None,
    user: AuthenticatedUser = Depends(get_current_user),
) -> SearchOut:
    excluded = [s for s in (exclude_source.split(",") if exclude_source else []) if s]
    with get_connection() as conn:
        result = search_service.search_articles(
            conn,
            q=q,
            page=page,
            date_from=date_from,
            date_to=date_to,
            lang=lang,
            status=status,
            exclude_sources=excluded,
        )
    return SearchOut(**result)
