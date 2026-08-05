"""Admin: manage the ~46 configured news outlets (fetch method(s),
language, enabled/disabled, test/edit/delete). See frontend-contract.md
§3.7 and §9 ("GET /sources", "POST /sources", "PATCH /sources/{id}",
"DELETE /sources/{id}", "POST /sources/{id}/test").

Not to be confused with app/sources/ (the fetch-plugin code layer) —
this router is the admin CRUD API over source *configuration rows*,
which is a distinct concern from the plugin classes that do the
fetching.

Stub only. TODO(ADMIN-API): implement, with server-side admin-role
enforcement (frontend-contract.md §8 — the mock only hides the nav
link, never blocks navigation; the real API must not repeat that gap).
"POST /sources/{id}/test" needs a live synchronous-feeling probe against
the P1-FETCH plugin for that source.
"""

from fastapi import APIRouter

router = APIRouter(prefix="/sources", tags=["admin", "sources"])
