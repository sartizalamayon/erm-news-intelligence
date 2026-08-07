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
    export,
    health,
    keywords,
    me,
    prompts,
    review,
    search,
    sources,
)

ALL_ROUTERS = [
    auth.router,
    me.router,
    dashboard.router,
    review.router,
    export.router,
    brief.router,
    brief.config_router,  # PUT /config/fuel-line — not under /brief's prefix, see brief.py's docstring.
    search.router,
    sources.router,
    keywords.router,
    prompts.router,
    health.router,
]
