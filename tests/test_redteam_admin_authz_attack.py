"""RED-TEAM round 3: attack ADMIN-API's auth claims by exploit, not by
reading its docstrings (commit f3f049e, 37 routes). Expired session,
role-escalation-via-body/header attempts, and provider-key plaintext
leak, driven end to end through real HTTP + a real migrated schema.

RESULT: all attacks held. No finding filed for this surface -- see
docs/red-team-findings.md's "held up" section.
"""

from __future__ import annotations

import contextlib

import pytest
from fastapi.testclient import TestClient

import app.api.deps as deps_module
import app.api.routers.auth as auth_router_module
import app.api.routers.prompts as prompts_router_module
from app.db.testing import migrated_schema, pg_available
from app.main import create_app
from app.services import auth as auth_service

pg_gated = pytest.mark.skipif(not pg_available(), reason="no reachable Postgres (set TEST_DATABASE_URL)")


@pytest.fixture(autouse=True)
def _secret_key(monkeypatch: pytest.MonkeyPatch):
    from cryptography.fernet import Fernet

    from app.config import get_settings

    monkeypatch.setenv("SECRET_KEY", Fernet.generate_key().decode())
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _wire(conn, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = lambda: contextlib.nullcontext(conn)  # noqa: E731
    monkeypatch.setattr(deps_module, "get_connection", fake)
    monkeypatch.setattr(auth_router_module, "get_connection", fake)
    monkeypatch.setattr(prompts_router_module, "get_connection", fake)


@pg_gated
def test_expired_session_rejected_on_admin_route(monkeypatch: pytest.MonkeyPatch) -> None:
    with migrated_schema() as conn:
        conn.execute("INSERT INTO users (email, role) VALUES ('admin@grameenphone.com','admin')")
        uid = conn.execute("SELECT id FROM users WHERE email='admin@grameenphone.com'").fetchone()["id"]
        token = auth_service.create_session(conn, user_id=uid, user_agent="t", ip_address="1.2.3.4")
        conn.execute(
            "UPDATE sessions SET expires_at = now() - interval '1 hour' WHERE token_hash = %s",
            (auth_service._hash_session_token(token),),
        )
        conn.commit()
        _wire(conn, monkeypatch)
        with TestClient(create_app()) as client:
            client.cookies.set(auth_service.SESSION_COOKIE_NAME, token)
            resp = client.get("/prompts/config")
        assert resp.status_code == 401


@pg_gated
def test_revoked_session_rejected_on_admin_route(monkeypatch: pytest.MonkeyPatch) -> None:
    with migrated_schema() as conn:
        conn.execute("INSERT INTO users (email, role) VALUES ('admin2@grameenphone.com','admin')")
        uid = conn.execute("SELECT id FROM users WHERE email='admin2@grameenphone.com'").fetchone()["id"]
        token = auth_service.create_session(conn, user_id=uid, user_agent="t", ip_address="1.2.3.4")
        auth_service.revoke_session(conn, token)
        conn.commit()
        _wire(conn, monkeypatch)
        with TestClient(create_app()) as client:
            client.cookies.set(auth_service.SESSION_COOKIE_NAME, token)
            resp = client.get("/prompts/config")
        assert resp.status_code == 401


@pg_gated
@pytest.mark.parametrize(
    "escalation_attempt",
    [
        {"role": "admin"},
        {"is_admin": True},
        {"user": {"role": "admin"}},
    ],
)
def test_role_escalation_via_request_body_is_ignored(monkeypatch: pytest.MonkeyPatch, escalation_attempt: dict) -> None:
    """An analyst session cannot self-escalate by smuggling a role/
    is_admin field into a request body -- role must come ONLY from the
    server-side session lookup (app.api.deps.get_current_user), never
    from anything client-supplied.
    """
    with migrated_schema() as conn:
        conn.execute("INSERT INTO users (email, role) VALUES ('analyst@grameenphone.com','analyst')")
        uid = conn.execute("SELECT id FROM users WHERE email='analyst@grameenphone.com'").fetchone()["id"]
        token = auth_service.create_session(conn, user_id=uid, user_agent="t", ip_address="1.2.3.4")
        conn.commit()
        _wire(conn, monkeypatch)
        with TestClient(create_app()) as client:
            client.cookies.set(auth_service.SESSION_COOKIE_NAME, token)
            resp = client.put("/prompts/guidance", json={"content": "x", **escalation_attempt})
        assert resp.status_code == 403, f"escalation body {escalation_attempt} got {resp.status_code}, expected 403"


@pg_gated
def test_role_escalation_via_header_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    with migrated_schema() as conn:
        conn.execute("INSERT INTO users (email, role) VALUES ('analyst2@grameenphone.com','analyst')")
        uid = conn.execute("SELECT id FROM users WHERE email='analyst2@grameenphone.com'").fetchone()["id"]
        token = auth_service.create_session(conn, user_id=uid, user_agent="t", ip_address="1.2.3.4")
        conn.commit()
        _wire(conn, monkeypatch)
        with TestClient(create_app()) as client:
            client.cookies.set(auth_service.SESSION_COOKIE_NAME, token)
            resp = client.get(
                "/prompts/config",
                headers={"X-Role": "admin", "X-User-Role": "admin", "X-Is-Admin": "true"},
            )
        assert resp.status_code == 403


@pg_gated
def test_provider_config_listing_never_returns_ciphertext_or_plaintext(monkeypatch: pytest.MonkeyPatch) -> None:
    with migrated_schema() as conn:
        conn.execute("INSERT INTO users (email, role) VALUES ('admin3@grameenphone.com','admin')")
        uid = conn.execute("SELECT id FROM users WHERE email='admin3@grameenphone.com'").fetchone()["id"]
        token = auth_service.create_session(conn, user_id=uid, user_agent="t", ip_address="1.2.3.4")
        conn.commit()
        from app.db.crypto import encrypt_api_key

        ciphertext, last4 = encrypt_api_key("sk-real-plaintext-secret-value-9999")
        conn.execute(
            "INSERT INTO provider_configs (provider, model, role, api_key_ciphertext, api_key_last4) "
            "VALUES ('groq', 'qwen/qwen3.6-27b', 'primary', %s, %s) "
            "ON CONFLICT (provider) DO UPDATE SET api_key_ciphertext = EXCLUDED.api_key_ciphertext, "
            "api_key_last4 = EXCLUDED.api_key_last4",
            (ciphertext, last4),
        )
        conn.commit()
        _wire(conn, monkeypatch)
        with TestClient(create_app()) as client:
            client.cookies.set(auth_service.SESSION_COOKIE_NAME, token)
            resp = client.get("/prompts/providers")
        assert resp.status_code == 200
        body = resp.text
        assert "sk-real-plaintext-secret-value-9999" not in body
        assert ciphertext.hex() not in body
