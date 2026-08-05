"""Admin: manage the ~183 bilingual (EN/BN) keyword terms across 5
sections; "rematch today" re-runs phase 2 against already-fetched
articles. See frontend-contract.md §3.8 and §9 ("GET /keywords?section=",
"POST /keywords", "PATCH /keywords/{id}", "DELETE /keywords/{id}",
"POST /keywords/rematch").

Stub only. TODO(ADMIN-API), with the same server-side admin-role
enforcement note as app/api/routers/sources.py.
"""

from fastapi import APIRouter

router = APIRouter(prefix="/keywords", tags=["admin", "keywords"])
