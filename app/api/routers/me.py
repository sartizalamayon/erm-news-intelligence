"""GET /me -- current authenticated user, for the global shell contract
(frontend-contract.md §2: `isAdmin`, `initials`, `userName`, `userEmail`,
all "Server -- derived from the authenticated user's role").

`GET /runs/current` and `POST /runs/current/cancel` (also named in the
original stub docstring) are NOT implemented here -- they are
run/job-status plumbing owned by P1-FETCH/ARCH-DB per
frontend-contract.md §2 and §7, outside AUTH-OTP's mission (OTP auth,
sessions, roles). Left for whoever picks that up next.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.deps import AuthenticatedUser, get_current_user
from app.api.schemas.auth import MeResponse

router = APIRouter(tags=["shell"])


def _name_parts(basis: str) -> list[str]:
    return [p for p in basis.replace(".", " ").replace("_", " ").replace("-", " ").split() if p]


def _initials(display_name: str, email: str) -> str:
    basis = (display_name or "").strip() or email.split("@", 1)[0]
    parts = _name_parts(basis)
    if not parts:
        return "?"
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][0] + parts[-1][0]).upper()


@router.get("/me", response_model=MeResponse)
def get_me(user: AuthenticatedUser = Depends(get_current_user)) -> MeResponse:
    return MeResponse(
        email=user.email,
        display_name=user.display_name or user.email.split("@", 1)[0],
        initials=_initials(user.display_name, user.email),
        role=user.role,
        is_admin=user.is_admin,
    )
