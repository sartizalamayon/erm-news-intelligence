"""Shared FastAPI dependencies (`Depends(...)`).

`get_current_user` resolves the session cookie (app.services.auth's
opaque-token design -- see that module's docstring for the session
mechanism and why) into an `AuthenticatedUser`; `require_admin` builds on
it for DoD #7's "roles enforced at the ROUTE layer" requirement.

frontend-contract.md §8 / §1 is explicit about why route-layer
enforcement is the ONLY real control here: "nothing in the markup or
script actually blocks navigation to these screens for a non-admin...
This is nav-hiding only, not access control. The real backend must
enforce role checks server-side on every admin-surface endpoint." Every
future admin-only router (ADMIN-API, "the next wave" per the mission
brief) should depend on `require_admin`, not re-derive its own role
check.

Deliberately raises plain `fastapi.HTTPException` for 401/403 rather
than extending `app.errors`' `AppError` taxonomy: that module's own
docstring reserves adding a new top-level exception category for
ARCH-REPO ("that's an ARCH-REPO-level change... not a per-agent one"),
and HTTPException is the standard, already-supported-by-Starlette
mechanism for exactly this -- no change to app/errors.py or its
registered handlers is needed.
"""

from __future__ import annotations

from fastapi import Depends, HTTPException, Request, status

from app.db.connection import get_connection
from app.services.auth import SESSION_COOKIE_NAME, AuthenticatedUser, get_session_user

__all__ = ["AuthenticatedUser", "get_current_user", "require_admin"]


def get_current_user(request: Request) -> AuthenticatedUser:
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated.")

    with get_connection() as conn:
        user = get_session_user(conn, token)

    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Session expired or invalid.")
    return user


def require_admin(user: AuthenticatedUser = Depends(get_current_user)) -> AuthenticatedUser:
    if not user.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin role required.")
    return user
