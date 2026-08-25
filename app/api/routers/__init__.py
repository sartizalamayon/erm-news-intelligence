"""API routers. Each module below is a stub — an APIRouter with no routes
yet, wired into app.main.create_app() so the path/prefix is reserved and
later agents only need to add `@router.get(...)` etc, not touch main.py.

`ALL_ROUTERS` is the single list app.main iterates; add a new router
module's `.router` to it when that module gains real routes.
"""

from app.api.routers import (
    auth,
    brief,
    dashboard,
    dashboard_long,
    export,
    health,
    keywords,
    me,
    prompts,
    review,
    search,
    sources,
    users,
)

ALL_ROUTERS = [
    auth.router,
    me.router,
    dashboard.router,
    # The rebuilt Long-term tabs. Separate module, same app: dashboard.py
    # keeps the Short-term tab and the storage-admin routes. The original
    # single-payload /dashboard/trends that used to sit alongside them
    # has been removed — these four endpoints replaced it in the shipped
    # frontend, and it had no callers left. See the note at the foot of
    # app/api/routers/dashboard.py.
    dashboard_long.router,
    review.router,
    export.router,
    brief.router,
    brief.config_router,  # PUT /config/fuel-line — not under /brief's prefix, see brief.py's docstring.
    search.router,
    sources.router,
    keywords.router,
    prompts.router,
    health.router,
    users.router,
]
