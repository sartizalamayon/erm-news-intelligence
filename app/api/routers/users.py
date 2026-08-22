"""Admin: user management — list/create/role-change/deactivate accounts
directly, bypassing OTP self-signup (app.services.auth). Every route is
`require_admin`-gated, same as every other admin router.

An admin can never act on their OWN account here (self-demote,
self-deactivate) — a narrower, cheaper guard than the "last admin"
invariant `app.services.users` enforces, and it exists for a different
reason: it stops an admin from accidentally locking themselves out of
their own current session even when other admins exist to take over.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from app.api.deps import AuthenticatedUser, require_admin
from app.api.schemas.admin import UserActiveUpdate, UserCreate, UserOut, UserRoleUpdate
from app.db.connection import get_connection
from app.errors import ValidationError
from app.services import users as users_service

router = APIRouter(prefix="/users", tags=["admin", "users"])


@router.get("", response_model=list[UserOut])
def list_users(admin: AuthenticatedUser = Depends(require_admin)) -> list[UserOut]:
    with get_connection() as conn:
        rows = users_service.list_users(conn)
    return [UserOut(**row) for row in rows]


@router.post("", response_model=UserOut, status_code=status.HTTP_201_CREATED)
def create_user(body: UserCreate, admin: AuthenticatedUser = Depends(require_admin)) -> UserOut:
    try:
        with get_connection() as conn:
            row = users_service.create_user(conn, email=body.email, password=body.password, role=body.role)
    except ValidationError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    return UserOut(**row)


@router.patch("/{user_id}/role", response_model=UserOut)
def update_user_role(
    user_id: int, body: UserRoleUpdate, admin: AuthenticatedUser = Depends(require_admin)
) -> UserOut:
    if user_id == admin.id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="cannot change your own role")
    try:
        with get_connection() as conn:
            row = users_service.set_user_role(conn, user_id, body.role)
    except ValidationError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="user not found")
    return UserOut(**row)


@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_user(user_id: int, admin: AuthenticatedUser = Depends(require_admin)) -> None:
    if user_id == admin.id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="cannot delete your own account")
    try:
        with get_connection() as conn:
            deleted = users_service.delete_user(conn, user_id)
    except ValidationError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="user not found")


@router.patch("/{user_id}/active", response_model=UserOut)
def update_user_active(
    user_id: int, body: UserActiveUpdate, admin: AuthenticatedUser = Depends(require_admin)
) -> UserOut:
    if user_id == admin.id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="cannot deactivate your own account")
    try:
        with get_connection() as conn:
            row = users_service.set_user_active(conn, user_id, body.is_active)
    except ValidationError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="user not found")
    return UserOut(**row)
