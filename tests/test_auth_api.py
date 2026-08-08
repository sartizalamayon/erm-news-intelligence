"""Router/HTTP-level tests for app.api.routers.auth and app.api.routers.me.

Two tiers, deliberately separated:

1. Exception-to-HTTP-status mapping and route-layer role/auth enforcement
   (DoD #7) -- tested with app.services.auth's functions monkeypatched
   and `get_connection` replaced with a no-op context manager, so these
   run with NO real database at all. The actual security *logic* (rate
   limiting, OTP correctness, password strength, enumeration, ...) is
   exhaustively covered at the service layer in tests/test_auth_service.py
   -- duplicating that here would just be slower, flakier versions of
   the same assertions. What's genuinely router-specific and worth
   testing in isolation is: does a `ValidationError` really come back as
   422, does a rate-limit exception really come back as 429, does
   `require_admin`/`get_current_user` really block an unauthenticated or
   non-admin caller at the route layer before the handler body ever runs.

2. A handful of real end-to-end tests (pg_available-gated) that drive
   the actual HTTP routes against a real, migrated schema (via
   app.db.testing.migrated_schema, monkeypatched in place of
   app.db.connection.get_connection) with app.services.email.send_otp_email
   mocked -- proving the whole stack (signup -> session cookie -> GET
   /me -> logout, and login, and change-password) actually wires
   together, not just each piece in isolation.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

import app.api.deps as deps_module
import app.api.routers.auth as auth_router_module
import app.services.auth as auth_service
from app.api.deps import AuthenticatedUser, get_current_user
from app.db.testing import migrated_schema, pg_available
from app.errors import ProviderPermanentError, ProviderRateLimitedError, ValidationError
from app.main import create_app

ALLOWED_EMAIL = "person@grameenphone.com"

# Meets PASSWORD_MIN_LENGTH (12), not in the weak-value blocklist, and
# does not contain ALLOWED_EMAIL's local part ("person").
STRONG_PASSWORD = "Zx9!Qwerty42Lmn"
STRONG_PASSWORD_2 = "Bq7#Falcon88Rvw"


@pytest.fixture(autouse=True)
def _secret_key(monkeypatch: pytest.MonkeyPatch):
    """See tests/test_auth_service.py's identical fixture docstring --
    tests/conftest.py deliberately sets SECRET_KEY="", but the real OTP
    code path (exercised by the Tier 2 end-to-end tests below, and
    reachable in Tier 1 whenever a test doesn't fully mock
    app.services.auth) needs one configured.
    """
    from app.config import get_settings

    monkeypatch.setenv("SECRET_KEY", "test-only-secret-key-do-not-use-in-prod")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# ── Tier 1: router mapping + route-layer role/auth enforcement, no DB ────


@pytest.fixture
def no_db_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """A TestClient whose auth router never opens a real database
    connection -- app.api.routers.auth.get_connection is replaced with a
    no-op context manager yielding None. Only safe to use alongside also
    monkeypatching the app.services.auth function(s) a given test's
    route will call, so that None connection is never actually touched.
    """
    monkeypatch.setattr(auth_router_module, "get_connection", lambda: contextlib.nullcontext(None))
    with TestClient(create_app()) as client:
        yield client


def test_otp_request_malformed_email_returns_422(client) -> None:
    resp = client.post("/auth/otp/request", json={"email": "not-an-email"})
    assert resp.status_code == 422


def test_password_set_malformed_code_returns_422(client) -> None:
    resp = client.post(
        "/auth/password/set", json={"email": ALLOWED_EMAIL, "code": "12", "new_password": STRONG_PASSWORD}
    )
    assert resp.status_code == 422


def test_login_malformed_email_returns_422(client) -> None:
    resp = client.post("/auth/login", json={"email": "not-an-email", "password": "whatever"})
    assert resp.status_code == 422


def test_otp_request_success_returns_202_with_generic_message(
    no_db_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(auth_service, "request_otp", Mock(return_value=None))
    resp = no_db_client.post("/auth/otp/request", json={"email": ALLOWED_EMAIL})
    assert resp.status_code == 202
    assert resp.json() == {"message": "If that address is eligible, a verification code has been sent."}


def test_otp_request_default_purpose_is_signup(no_db_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = Mock(return_value=None)
    monkeypatch.setattr(auth_service, "request_otp", fake)
    no_db_client.post("/auth/otp/request", json={"email": ALLOWED_EMAIL})
    assert fake.call_args.kwargs["purpose"] == "signup"


def test_otp_request_accepts_password_reset_purpose(no_db_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = Mock(return_value=None)
    monkeypatch.setattr(auth_service, "request_otp", fake)
    resp = no_db_client.post("/auth/otp/request", json={"email": ALLOWED_EMAIL, "purpose": "password_reset"})
    assert resp.status_code == 202
    assert fake.call_args.kwargs["purpose"] == "password_reset"


def test_otp_request_rejects_invalid_purpose(no_db_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(auth_service, "request_otp", Mock(return_value=None))
    resp = no_db_client.post("/auth/otp/request", json={"email": ALLOWED_EMAIL, "purpose": "reset-my-whole-account"})
    assert resp.status_code == 422


def test_otp_request_rate_limited_returns_429(no_db_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        auth_service, "request_otp", Mock(side_effect=auth_service.OtpRateLimited("slow down"))
    )
    resp = no_db_client.post("/auth/otp/request", json={"email": ALLOWED_EMAIL})
    assert resp.status_code == 429


def test_otp_request_disallowed_domain_returns_422_with_domain_message(
    no_db_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        auth_service,
        "request_otp",
        Mock(side_effect=ValidationError("Access is limited to @grameenphone.com addresses.")),
    )
    resp = no_db_client.post("/auth/otp/request", json={"email": "person@gmail.com"})
    assert resp.status_code == 422
    assert "grameenphone.com" in resp.json()["detail"]


def test_otp_request_quota_exhausted_returns_503(no_db_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        auth_service, "request_otp", Mock(side_effect=ProviderRateLimitedError("quota", provider="brevo"))
    )
    resp = no_db_client.post("/auth/otp/request", json={"email": ALLOWED_EMAIL})
    assert resp.status_code == 503
    body = resp.json()
    assert body["retryable"] is True
    assert body["provider"] == "brevo"


def test_otp_request_permanent_send_failure_returns_502(
    no_db_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        auth_service, "request_otp", Mock(side_effect=ProviderPermanentError("rejected", provider="brevo"))
    )
    resp = no_db_client.post("/auth/otp/request", json={"email": ALLOWED_EMAIL})
    assert resp.status_code == 502


def test_password_set_invalid_code_returns_422(no_db_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        auth_service, "set_password", Mock(side_effect=ValidationError("Invalid or expired verification code."))
    )
    resp = no_db_client.post(
        "/auth/password/set", json={"email": ALLOWED_EMAIL, "code": "123456", "new_password": STRONG_PASSWORD}
    )
    assert resp.status_code == 422


def test_password_set_weak_password_returns_422(no_db_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        auth_service, "set_password", Mock(side_effect=ValidationError("Password must be at least 12 characters."))
    )
    resp = no_db_client.post(
        "/auth/password/set", json={"email": ALLOWED_EMAIL, "code": "123456", "new_password": "short1!"}
    )
    assert resp.status_code == 422


def test_login_wrong_credentials_returns_422(no_db_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        auth_service, "login", Mock(side_effect=ValidationError("Invalid email or password."))
    )
    resp = no_db_client.post("/auth/login", json={"email": ALLOWED_EMAIL, "password": "wrong-one"})
    assert resp.status_code == 422


def test_login_rate_limited_returns_429(no_db_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        auth_service, "login", Mock(side_effect=auth_service.LoginRateLimited("slow down"))
    )
    resp = no_db_client.post("/auth/login", json={"email": ALLOWED_EMAIL, "password": "whatever12345"})
    assert resp.status_code == 429


def test_me_without_session_cookie_returns_401(client) -> None:
    resp = client.get("/me")
    assert resp.status_code == 401


def test_otp_failures_without_session_returns_401(client) -> None:
    resp = client.get("/auth/otp-failures")
    assert resp.status_code == 401


def test_change_password_without_session_returns_401(client) -> None:
    resp = client.post(
        "/auth/password/change", json={"current_password": STRONG_PASSWORD, "new_password": STRONG_PASSWORD_2}
    )
    assert resp.status_code == 401


def test_change_password_wrong_current_password_returns_422(monkeypatch: pytest.MonkeyPatch) -> None:
    app = create_app()
    user = AuthenticatedUser(id=7, email=ALLOWED_EMAIL, role="analyst", display_name="P", session_id=9)
    app.dependency_overrides[get_current_user] = lambda: user
    monkeypatch.setattr(auth_router_module, "get_connection", lambda: contextlib.nullcontext(None))
    monkeypatch.setattr(
        auth_service, "change_password", Mock(side_effect=ValidationError("Current password is incorrect."))
    )
    with TestClient(app) as client:
        resp = client.post(
            "/auth/password/change",
            json={"current_password": "wrong", "new_password": STRONG_PASSWORD_2},
        )
    assert resp.status_code == 422


def test_change_password_rate_limited_returns_429(monkeypatch: pytest.MonkeyPatch) -> None:
    app = create_app()
    user = AuthenticatedUser(id=7, email=ALLOWED_EMAIL, role="analyst", display_name="P", session_id=9)
    app.dependency_overrides[get_current_user] = lambda: user
    monkeypatch.setattr(auth_router_module, "get_connection", lambda: contextlib.nullcontext(None))
    monkeypatch.setattr(
        auth_service, "change_password", Mock(side_effect=auth_service.PasswordChangeRateLimited("slow down"))
    )
    with TestClient(app) as client:
        resp = client.post(
            "/auth/password/change",
            json={"current_password": STRONG_PASSWORD, "new_password": STRONG_PASSWORD_2},
        )
    assert resp.status_code == 429


def test_otp_failures_as_analyst_is_forbidden_at_route_layer() -> None:
    """DoD #7: role enforcement happens at the route layer via the
    reusable require_admin dependency, independent of anything the
    frontend does. Overriding get_current_user (not touching the
    database at all) proves require_admin itself is the thing doing the
    blocking, exactly like ADMIN-API's future admin routers will rely on.
    """
    app = create_app()
    analyst = AuthenticatedUser(id=1, email=ALLOWED_EMAIL, role="analyst", display_name="Analyst", session_id=1)
    app.dependency_overrides[get_current_user] = lambda: analyst
    with TestClient(app) as client:
        resp = client.get("/auth/otp-failures")
    assert resp.status_code == 403


def test_me_reflects_admin_role_and_initials() -> None:
    app = create_app()
    admin = AuthenticatedUser(
        id=2, email="jane.doe@grameenphone.com", role="admin", display_name="Jane Doe", session_id=2
    )
    app.dependency_overrides[get_current_user] = lambda: admin
    with TestClient(app) as client:
        resp = client.get("/me")
    assert resp.status_code == 200
    body = resp.json()
    assert body["is_admin"] is True
    assert body["role"] == "admin"
    assert body["initials"] == "JD"
    assert body["email"] == "jane.doe@grameenphone.com"


def test_logout_clears_cookie(no_db_client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(auth_service, "revoke_session", Mock(return_value=None))
    no_db_client.cookies.set(auth_service.SESSION_COOKIE_NAME, "some-token")
    resp = no_db_client.post("/auth/logout")
    assert resp.status_code == 204
    set_cookie = resp.headers.get("set-cookie", "")
    assert auth_service.SESSION_COOKIE_NAME in set_cookie
    assert "Max-Age=0" in set_cookie or "expires=" in set_cookie.lower()


# ── RT-5 fix: _client_ip ignores X-Forwarded-For unless a trusted hop
# count says otherwise (docs/red-team-findings.md) ───────────────────────


class _FakeClient:
    def __init__(self, host: str) -> None:
        self.host = host


class _FakeRequest:
    """Minimal stand-in for fastapi.Request -- _client_ip only ever
    touches `.headers.get(...)` and `.client.host`, so a real Request
    (which needs a full ASGI scope) is unnecessary for these unit tests.
    """

    def __init__(self, headers: dict[str, str] | None = None, client_host: str | None = "192.0.2.1") -> None:
        self.headers = headers or {}
        self.client = _FakeClient(client_host) if client_host is not None else None


@pytest.fixture(autouse=True)
def _no_trusted_proxy_hops_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test in this file should see the real default (0 trusted
    hops) unless it explicitly opts into a different value -- guards
    against a stray TRUSTED_PROXY_HOPS in the ambient environment
    leaking into a test that didn't ask for it.
    """
    monkeypatch.delenv("TRUSTED_PROXY_HOPS", raising=False)


def test_client_ip_ignores_spoofed_header_by_default() -> None:
    request = _FakeRequest(headers={"x-forwarded-for": "1.2.3.4"}, client_host="192.0.2.1")
    assert auth_router_module._client_ip(request) == "192.0.2.1"


def test_client_ip_ignores_spoofed_multi_hop_chain_by_default() -> None:
    # A fully attacker-controlled chain, forged to LOOK like it went
    # through several proxies -- with the default of 0 trusted hops,
    # none of it is consulted; only the direct socket peer is used.
    request = _FakeRequest(
        headers={"x-forwarded-for": "9.9.9.9, 8.8.8.8, 7.7.7.7, 6.6.6.6"}, client_host="192.0.2.1"
    )
    assert auth_router_module._client_ip(request) == "192.0.2.1"


def test_client_ip_trusts_rightmost_entry_with_one_trusted_hop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRUSTED_PROXY_HOPS", "1")
    # "<attacker-supplied-prefix>, <real-client-as-seen-by-the-one-trusted-proxy>"
    request = _FakeRequest(headers={"x-forwarded-for": "203.0.113.66, 198.51.100.1"}, client_host="10.0.0.5")
    assert auth_router_module._client_ip(request) == "198.51.100.1"


def test_client_ip_trusts_nth_from_right_with_multiple_trusted_hops(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRUSTED_PROXY_HOPS", "2")
    # Chain: <attacker-prefix>, <real client, appended by trusted hop #1>,
    # <trusted hop #1's own address, appended by trusted hop #2>.
    request = _FakeRequest(
        headers={"x-forwarded-for": "203.0.113.66, 198.51.100.1, 198.51.100.2"}, client_host="10.0.0.5"
    )
    assert auth_router_module._client_ip(request) == "198.51.100.1"


def test_client_ip_falls_back_to_peer_when_hops_configured_but_header_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRUSTED_PROXY_HOPS", "1")
    request = _FakeRequest(headers={}, client_host="10.0.0.5")
    assert auth_router_module._client_ip(request) == "10.0.0.5"


def test_client_ip_falls_back_to_peer_when_chain_shorter_than_configured_hops(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRUSTED_PROXY_HOPS", "3")
    # Only 1 entry present but 3 hops configured -- can't possibly be the
    # trusted hop's own append, so this must not be trusted blindly.
    request = _FakeRequest(headers={"x-forwarded-for": "203.0.113.66"}, client_host="10.0.0.5")
    assert auth_router_module._client_ip(request) == "10.0.0.5"


def test_client_ip_invalid_trusted_proxy_hops_value_defaults_to_safe_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRUSTED_PROXY_HOPS", "not-a-number")
    request = _FakeRequest(headers={"x-forwarded-for": "1.2.3.4"}, client_host="192.0.2.1")
    assert auth_router_module._client_ip(request) == "192.0.2.1"


def test_spoofed_forwarded_header_no_longer_bypasses_per_ip_rate_limit_over_http(
    no_db_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """HTTP-level companion to the RT-5 xfail proof
    (tests/test_redteam_otp_rate_limit_bypass.py) -- with the default 0
    trusted hops, request_otp() is always called with the TestClient's
    one real connecting IP, regardless of how many distinct forged
    X-Forwarded-For chains are sent, so a real deployment's rate limiter
    sees one consistent identity to count against rather than an
    attacker-chosen new one per request.
    """
    seen_ips: list[str] = []

    def _capture(conn, *, email, ip_address, purpose="signup"):  # noqa: ANN001 -- matches auth_service.request_otp's signature loosely for this fake
        seen_ips.append(ip_address)

    monkeypatch.setattr(auth_service, "request_otp", _capture)

    forged_chains = [
        "9.9.9.9",
        "8.8.8.8, 7.7.7.7",
        "6.6.6.6, 5.5.5.5, 4.4.4.4",
    ]
    for i, chain in enumerate(forged_chains):
        resp = no_db_client.post(
            "/auth/otp/request",
            json={"email": f"person{i}@grameenphone.com"},
            headers={"X-Forwarded-For": chain},
        )
        assert resp.status_code == 202

    assert len(set(seen_ips)) == 1, f"expected every request to resolve to the same real peer IP, got {seen_ips}"
    assert seen_ips[0] not in {"9.9.9.9", "8.8.8.8", "7.7.7.7", "6.6.6.6", "5.5.5.5", "4.4.4.4"}


# ── Tier 2: real end-to-end flow against a migrated schema ───────────────

pg_gated = pytest.mark.skipif(not pg_available(), reason="no reachable Postgres (set TEST_DATABASE_URL)")


@pg_gated
def test_full_signup_flow_end_to_end(monkeypatch: pytest.MonkeyPatch) -> None:
    with migrated_schema() as conn:
        monkeypatch.setattr(auth_router_module, "get_connection", lambda: contextlib.nullcontext(conn))
        monkeypatch.setattr(deps_module, "get_connection", lambda: contextlib.nullcontext(conn))

        fake_send = Mock(return_value=None)
        monkeypatch.setattr(auth_service, "send_otp_email", fake_send)

        with TestClient(create_app()) as client:
            resp = client.post("/auth/otp/request", json={"email": ALLOWED_EMAIL})
            assert resp.status_code == 202
            assert fake_send.call_count == 1
            code = fake_send.call_args.kwargs["code"]

            # A wrong code first -- must not lock anyone out or leak state.
            wrong = client.post(
                "/auth/password/set",
                json={"email": ALLOWED_EMAIL, "code": "000000", "new_password": STRONG_PASSWORD},
            )
            assert wrong.status_code == 422

            resp = client.post(
                "/auth/password/set",
                json={"email": ALLOWED_EMAIL, "code": code, "new_password": STRONG_PASSWORD},
            )
            assert resp.status_code == 200
            body = resp.json()
            assert body["email"] == ALLOWED_EMAIL
            assert body["role"] == "analyst"

            set_cookie = resp.headers.get("set-cookie", "")
            assert auth_service.SESSION_COOKIE_NAME in set_cookie
            assert "httponly" in set_cookie.lower()

            me = client.get("/me")
            assert me.status_code == 200
            assert me.json()["email"] == ALLOWED_EMAIL
            assert me.json()["is_admin"] is False

            # Reusing the same (now-consumed) code must fail.
            replay = client.post(
                "/auth/password/set",
                json={"email": ALLOWED_EMAIL, "code": code, "new_password": STRONG_PASSWORD_2},
            )
            assert replay.status_code == 422

            logout_resp = client.post("/auth/logout")
            assert logout_resp.status_code == 204

            after_logout = client.get("/me")
            assert after_logout.status_code == 401

            # And the password set during signup now works via /auth/login.
            login_resp = client.post("/auth/login", json={"email": ALLOWED_EMAIL, "password": STRONG_PASSWORD})
            assert login_resp.status_code == 200
            assert login_resp.json()["email"] == ALLOWED_EMAIL


@pg_gated
def test_login_wrong_password_returns_422_end_to_end(monkeypatch: pytest.MonkeyPatch) -> None:
    with migrated_schema() as conn:
        conn.execute("INSERT INTO users (email, role) VALUES (%s, 'analyst')", (ALLOWED_EMAIL,))
        conn.execute(
            "UPDATE users SET password_hash = %s WHERE email = %s",
            (auth_service.hash_password(STRONG_PASSWORD), ALLOWED_EMAIL),
        )
        monkeypatch.setattr(auth_router_module, "get_connection", lambda: contextlib.nullcontext(conn))

        with TestClient(create_app()) as client:
            resp = client.post("/auth/login", json={"email": ALLOWED_EMAIL, "password": "not-the-password"})
        assert resp.status_code == 422
        assert "set-cookie" not in resp.headers  # no session issued on a failed login


@pg_gated
def test_change_password_end_to_end_evicts_other_session(monkeypatch: pytest.MonkeyPatch) -> None:
    with migrated_schema() as conn:
        conn.execute("INSERT INTO users (email, role) VALUES (%s, 'analyst')", (ALLOWED_EMAIL,))
        conn.execute(
            "UPDATE users SET password_hash = %s WHERE email = %s",
            (auth_service.hash_password(STRONG_PASSWORD), ALLOWED_EMAIL),
        )
        user_row = conn.execute("SELECT id FROM users WHERE email = %s", (ALLOWED_EMAIL,)).fetchone()
        other_token = auth_service.create_session(
            conn, user_id=user_row["id"], user_agent="attacker-device", ip_address="203.0.113.66"
        )

        monkeypatch.setattr(auth_router_module, "get_connection", lambda: contextlib.nullcontext(conn))
        monkeypatch.setattr(deps_module, "get_connection", lambda: contextlib.nullcontext(conn))

        with TestClient(create_app()) as client:
            login_resp = client.post("/auth/login", json={"email": ALLOWED_EMAIL, "password": STRONG_PASSWORD})
            assert login_resp.status_code == 200

            change_resp = client.post(
                "/auth/password/change",
                json={"current_password": STRONG_PASSWORD, "new_password": STRONG_PASSWORD_2},
            )
            assert change_resp.status_code == 204

            # Current (browser) session still authenticated.
            me = client.get("/me")
            assert me.status_code == 200

        # The OTHER session (simulating an attacker's stolen token) is dead.
        assert auth_service.get_session_user(conn, other_token) is None


@pg_gated
def test_admin_can_list_otp_failures_end_to_end(monkeypatch: pytest.MonkeyPatch) -> None:
    with migrated_schema() as conn:
        conn.execute(
            "INSERT INTO users (email, role, display_name) VALUES (%s, 'admin', 'Admin User')", (ALLOWED_EMAIL,)
        )
        user_row = conn.execute("SELECT id FROM users WHERE email = %s", (ALLOWED_EMAIL,)).fetchone()
        token = auth_service.create_session(
            conn, user_id=user_row["id"], user_agent="pytest", ip_address="203.0.113.5"
        )
        conn.execute(
            "INSERT INTO otp_codes (email, code_hash, expires_at, requested_ip, send_status, send_error) "
            "VALUES (%s, 'x', now() + interval '10 minutes', '203.0.113.6', 'failed', 'Provider rejected it')",
            ("someone.else@grameenphone.com",),
        )

        monkeypatch.setattr(auth_router_module, "get_connection", lambda: contextlib.nullcontext(conn))
        monkeypatch.setattr(deps_module, "get_connection", lambda: contextlib.nullcontext(conn))

        with TestClient(create_app()) as client:
            client.cookies.set(auth_service.SESSION_COOKIE_NAME, token)
            resp = client.get("/auth/otp-failures")

        assert resp.status_code == 200
        rows = resp.json()
        assert len(rows) == 1
        assert rows[0]["send_status"] == "failed"
        # Masked, never the full address (DoD #8's spirit applied to an
        # admin-facing view too -- see this package's task report for
        # the reasoning/trade-off).
        assert "someone.else@grameenphone.com" not in rows[0]["email_masked"]


@pg_gated
def test_non_admin_session_cannot_list_otp_failures_end_to_end(monkeypatch: pytest.MonkeyPatch) -> None:
    with migrated_schema() as conn:
        conn.execute("INSERT INTO users (email, role) VALUES (%s, 'analyst')", (ALLOWED_EMAIL,))
        user_row = conn.execute("SELECT id FROM users WHERE email = %s", (ALLOWED_EMAIL,)).fetchone()
        token = auth_service.create_session(conn, user_id=user_row["id"], user_agent="pytest", ip_address="203.0.113.5")

        monkeypatch.setattr(auth_router_module, "get_connection", lambda: contextlib.nullcontext(conn))
        monkeypatch.setattr(deps_module, "get_connection", lambda: contextlib.nullcontext(conn))

        with TestClient(create_app()) as client:
            client.cookies.set(auth_service.SESSION_COOKIE_NAME, token)
            resp = client.get("/auth/otp-failures")

        assert resp.status_code == 403
