"""Admin user management: list/create/role-change/deactivate accounts
directly, bypassing the OTP self-signup flow (app.services.auth).

Reuses every auth primitive rather than re-implementing anything:
`hash_password`/`_validate_password_strength`/`is_domain_allowed` from
app.services.auth are the exact same functions the self-signup path
calls, so an admin-created account is validated identically to a
self-signed-up one — same domain restriction, same password strength
floor, same Argon2id hashing. The one deliberate difference: an
admin-created account gets its password set immediately, no OTP round
trip, since the admin is themselves the verified-identity check.

Last-admin protection: `set_user_role` and `set_user_active` both
refuse an action that would leave zero active admins, so an admin can
never lock every admin (including themselves) out of the app.
"""

from __future__ import annotations

from typing import Any

from app.db.connection import DictConnection
from app.errors import ValidationError
from app.services.auth import (
    _validate_password_strength,
    hash_password,
    is_domain_allowed,
    normalize_email,
)

ROLE_ADMIN = "admin"  # matches users.role's CHECK constraint (migrations/0005_auth.sql), same literal auth.py uses
ROLES = (ROLE_ADMIN, "analyst")


def _user_row_to_dict(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "email": row["email"],
        "role": row["role"],
        "display_name": row["display_name"],
        "is_active": row["is_active"],
        "created_at": row["created_at"].isoformat() if row.get("created_at") else None,
        "last_login_at": row["last_login_at"].isoformat() if row.get("last_login_at") else None,
        "has_password": bool(row["password_hash"]),
    }


def list_users(conn: DictConnection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT id, email, role, display_name, is_active, created_at, last_login_at, password_hash "
        "FROM users ORDER BY created_at DESC"
    ).fetchall()
    return [_user_row_to_dict(dict(r)) for r in rows]


def _active_admin_count(conn: DictConnection, *, excluding_user_id: int | None = None) -> int:
    if excluding_user_id is None:
        row = conn.execute(
            "SELECT count(*) AS n FROM users WHERE role = %s AND is_active", (ROLE_ADMIN,)
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT count(*) AS n FROM users WHERE role = %s AND is_active AND id != %s",
            (ROLE_ADMIN, excluding_user_id),
        ).fetchone()
    assert row is not None
    return int(row["n"])


def create_user(conn: DictConnection, *, email: str, password: str, role: str) -> dict[str, Any]:
    if role not in ROLES:
        raise ValidationError(f"role must be one of {ROLES}")
    email = normalize_email(email)
    if not is_domain_allowed(conn, email):
        raise ValidationError("Email must be on the allowed domain.")
    existing = conn.execute("SELECT id FROM users WHERE email = %s", (email,)).fetchone()
    if existing is not None:
        raise ValidationError(f"a user with email {email!r} already exists")
    _validate_password_strength(password, email=email)
    password_hash = hash_password(password)
    row = conn.execute(
        "INSERT INTO users (email, role, password_hash, is_active) VALUES (%s, %s, %s, TRUE) "
        "RETURNING id, email, role, display_name, is_active, created_at, last_login_at, password_hash",
        (email, role, password_hash),
    ).fetchone()
    conn.commit()
    assert row is not None
    return _user_row_to_dict(dict(row))


def _get_user(conn: DictConnection, user_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT id, email, role, display_name, is_active, created_at, last_login_at, password_hash "
        "FROM users WHERE id = %s",
        (user_id,),
    ).fetchone()
    return dict(row) if row is not None else None


def set_user_role(conn: DictConnection, user_id: int, role: str) -> dict[str, Any] | None:
    if role not in ROLES:
        raise ValidationError(f"role must be one of {ROLES}")
    current = _get_user(conn, user_id)
    if current is None:
        return None
    if current["role"] == ROLE_ADMIN and role != ROLE_ADMIN and current["is_active"]:
        if _active_admin_count(conn, excluding_user_id=user_id) == 0:
            raise ValidationError("cannot demote the last active admin")
    row = conn.execute(
        "UPDATE users SET role = %s WHERE id = %s "
        "RETURNING id, email, role, display_name, is_active, created_at, last_login_at, password_hash",
        (role, user_id),
    ).fetchone()
    conn.commit()
    return _user_row_to_dict(dict(row)) if row is not None else None


def set_user_active(conn: DictConnection, user_id: int, is_active: bool) -> dict[str, Any] | None:
    current = _get_user(conn, user_id)
    if current is None:
        return None
    if not is_active and current["role"] == ROLE_ADMIN and current["is_active"]:
        if _active_admin_count(conn, excluding_user_id=user_id) == 0:
            raise ValidationError("cannot deactivate the last active admin")
    row = conn.execute(
        "UPDATE users SET is_active = %s WHERE id = %s "
        "RETURNING id, email, role, display_name, is_active, created_at, last_login_at, password_hash",
        (is_active, user_id),
    ).fetchone()
    conn.commit()
    return _user_row_to_dict(dict(row)) if row is not None else None


def delete_user(conn: DictConnection, user_id: int) -> bool:
    current = _get_user(conn, user_id)
    if current is None:
        return False
    if current["role"] == ROLE_ADMIN and current["is_active"]:
        if _active_admin_count(conn, excluding_user_id=user_id) == 0:
            raise ValidationError("cannot delete the last active admin")
    row = conn.execute("DELETE FROM users WHERE id = %s RETURNING id", (user_id,)).fetchone()
    conn.commit()
    return row is not None
