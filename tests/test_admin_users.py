"""app.services.users — admin user management: create/list/role-change/
deactivate accounts directly, bypassing OTP self-signup. Reuses
app.services.auth's domain check, password strength floor, and Argon2id
hashing, so an admin-created account is validated identically to a
self-signed-up one.

`migrated_schema()` always seeds one bootstrap admin
(nt.sartiz@grameenphone.com, migrations/0009_seed_admin.sql) — tests
that need to pin an exact admin count work with/around that account
rather than assuming a clean slate.
"""

from __future__ import annotations

import pytest

from app.db.testing import migrated_schema
from app.errors import ValidationError
from app.services import users as users_service

PW = "a-strong-enough-password-123"
SEEDED_ADMIN_EMAIL = "nt.sartiz@grameenphone.com"


def _seeded_admin_id(conn) -> int:
    row = conn.execute("SELECT id FROM users WHERE email = %s", (SEEDED_ADMIN_EMAIL,)).fetchone()
    assert row is not None
    return int(row["id"])


def test_create_user_reuses_domain_and_password_validation() -> None:
    with migrated_schema() as conn:
        with pytest.raises(ValidationError, match="allowed domain"):
            users_service.create_user(conn, email="new.person@gmail.com", password=PW, role="analyst")
        with pytest.raises(ValidationError):
            users_service.create_user(conn, email="new.person@grameenphone.com", password="short", role="analyst")


def test_create_user_rejects_duplicate_email_and_unknown_role() -> None:
    with migrated_schema() as conn:
        users_service.create_user(conn, email="new.person@grameenphone.com", password=PW, role="analyst")
        with pytest.raises(ValidationError, match="already exists"):
            users_service.create_user(conn, email="new.person@grameenphone.com", password=PW, role="analyst")
        with pytest.raises(ValidationError, match="role"):
            users_service.create_user(conn, email="another.person@grameenphone.com", password=PW, role="superadmin")


def test_create_user_sets_password_immediately_no_otp() -> None:
    with migrated_schema() as conn:
        row = users_service.create_user(conn, email="new.person@grameenphone.com", password=PW, role="admin")
        assert row["has_password"] is True
        assert row["is_active"] is True
        assert row["role"] == "admin"


def test_list_users_includes_the_seeded_admin_and_new_ones() -> None:
    with migrated_schema() as conn:
        users_service.create_user(conn, email="new.person@grameenphone.com", password=PW, role="analyst")
        emails = {u["email"] for u in users_service.list_users(conn)}
        assert emails == {SEEDED_ADMIN_EMAIL, "new.person@grameenphone.com"}


def test_set_user_role_changes_role() -> None:
    with migrated_schema() as conn:
        new_user = users_service.create_user(conn, email="new.person@grameenphone.com", password=PW, role="analyst")
        updated = users_service.set_user_role(conn, new_user["id"], "admin")
        assert updated["role"] == "admin"


def test_cannot_demote_the_last_active_admin() -> None:
    """The seeded admin, alone, is the only admin -- demoting them must
    be refused.
    """
    with migrated_schema() as conn:
        seeded_id = _seeded_admin_id(conn)
        with pytest.raises(ValidationError, match="last active admin"):
            users_service.set_user_role(conn, seeded_id, "analyst")


def test_demoting_one_of_two_admins_is_allowed() -> None:
    with migrated_schema() as conn:
        second_admin = users_service.create_user(conn, email="second.admin@grameenphone.com", password=PW, role="admin")
        updated = users_service.set_user_role(conn, second_admin["id"], "analyst")
        assert updated["role"] == "analyst"


def test_cannot_deactivate_the_last_active_admin() -> None:
    with migrated_schema() as conn:
        seeded_id = _seeded_admin_id(conn)
        with pytest.raises(ValidationError, match="last active admin"):
            users_service.set_user_active(conn, seeded_id, False)


def test_deactivating_one_of_two_admins_is_allowed() -> None:
    with migrated_schema() as conn:
        second_admin = users_service.create_user(conn, email="second.admin@grameenphone.com", password=PW, role="admin")
        updated = users_service.set_user_active(conn, second_admin["id"], False)
        assert updated["is_active"] is False


def test_deactivated_user_cannot_log_in() -> None:
    from app.services.auth import login

    with migrated_schema() as conn:
        analyst = users_service.create_user(conn, email="new.person@grameenphone.com", password=PW, role="analyst")
        users_service.set_user_active(conn, analyst["id"], False)
        with pytest.raises(ValidationError):
            login(conn, email="new.person@grameenphone.com", password=PW, ip_address="127.0.0.1")


def test_deactivated_user_loses_an_existing_session() -> None:
    from app.services.auth import create_session, get_session_user

    with migrated_schema() as conn:
        analyst = users_service.create_user(conn, email="new.person@grameenphone.com", password=PW, role="analyst")
        token = create_session(conn, user_id=analyst["id"], user_agent="pytest", ip_address="127.0.0.1")
        conn.commit()
        assert get_session_user(conn, token) is not None
        users_service.set_user_active(conn, analyst["id"], False)
        assert get_session_user(conn, token) is None


def test_operating_on_unknown_user_returns_none() -> None:
    with migrated_schema() as conn:
        assert users_service.set_user_role(conn, 999999, "admin") is None
        assert users_service.set_user_active(conn, 999999, False) is None


def test_delete_user_removes_them_from_the_list() -> None:
    with migrated_schema() as conn:
        new_user = users_service.create_user(conn, email="new.person@grameenphone.com", password=PW, role="analyst")
        assert users_service.delete_user(conn, new_user["id"]) is True
        emails = {u["email"] for u in users_service.list_users(conn)}
        assert emails == {SEEDED_ADMIN_EMAIL}


def test_deleting_unknown_user_returns_false() -> None:
    with migrated_schema() as conn:
        assert users_service.delete_user(conn, 999999) is False


def test_cannot_delete_the_last_active_admin() -> None:
    with migrated_schema() as conn:
        seeded_id = _seeded_admin_id(conn)
        with pytest.raises(ValidationError, match="last active admin"):
            users_service.delete_user(conn, seeded_id)
