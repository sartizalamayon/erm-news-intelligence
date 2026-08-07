"""Full-text search archive across every collected article (headline,
English translation, body), regardless of date/status. See
frontend-contract.md §3.6 and frontend/js/screens/search.js's header
comment for the exact shape this was built against.

Requires an authenticated session (`get_current_user`) — user-facing,
not admin-only.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.deps import AuthenticatedUser, get_current_user
from app.api.schemas.core import SearchOut
from app.db.connection import get_connection
from app.services import search as search_service

router = APIRouter(prefix="/search", tags=["search"])


@router.get("", response_model=SearchOut)
def search(q: str = "", page: int = 1, user: AuthenticatedUser = Depends(get_current_user)) -> SearchOut:
    with get_connection() as conn:
        result = search_service.search_articles(conn, q=q, page=page)
    return SearchOut(**result)
