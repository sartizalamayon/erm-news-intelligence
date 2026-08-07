"""app.api.error_handlers's HARDEN-mission additions (mission item 7:
"no stack traces, no SQL, no internal paths in production. Confirm
APP_ENV=production behavior explicitly."):

  * RequestValidationError (malformed JSON, a body that fails pydantic
    validation) -> the app's uniform {"error", "detail"} envelope, never
    FastAPI's default per-field body (which echoes the raw submitted
    value back).
  * A genuinely unmodeled exception (not an app.errors.AppError
    subclass) -> a generic 500 in the same envelope, full detail only in
    the server-side log (exc_info=...), identically whether APP_ENV is
    "test" or "production" -- proven directly below rather than assumed
    from FastAPI's debug=False default, which app.main.create_app() also
    sets explicitly for the same reason.
"""

from __future__ import annotations

import contextlib

import pytest
from fastapi.testclient import TestClient

from app.api.deps import AuthenticatedUser, get_current_user
from app.config import get_settings
from app.main import create_app

ADMIN = AuthenticatedUser(id=1, email="admin@grameenphone.com", role="admin", display_name="Admin", session_id=1)

_SECRET_MARKER = "boom-this-string-must-never-reach-the-client-b3ac9f"


def test_malformed_json_returns_uniform_422_envelope(client):
    response = client.post(
        "/auth/otp/request",
        content=b"{not valid json at all",
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 422
    assert response.json() == {"error": "validation_error", "detail": "The request body failed validation."}


def test_malformed_json_does_not_echo_the_submitted_body(client):
    weird_input = "not-json-but-contains-a-marker-9f3c1a"
    response = client.post(
        "/auth/otp/request",
        content=weird_input.encode(),
        headers={"Content-Type": "application/json"},
    )
    assert weird_input not in response.text


def test_wrong_field_type_returns_uniform_422_envelope(client):
    # code must be a string per app.api.schemas.auth.OtpVerifyBody --
    # sending an int fails pydantic validation (RequestValidationError),
    # not app.errors.ValidationError.
    response = client.post("/auth/otp/verify", json={"email": "a@grameenphone.com", "code": 123456})
    assert response.status_code == 422
    assert response.json() == {"error": "validation_error", "detail": "The request body failed validation."}


def _fake_db_connection():
    @contextlib.contextmanager
    def _cm(dsn=None):
        yield object()

    return _cm


def test_unmodeled_exception_returns_generic_500(client, monkeypatch: pytest.MonkeyPatch):
    import app.api.routers.health as health_router_module
    import app.services.admin as admin_service_module

    def _raise(conn, limit):
        raise RuntimeError(_SECRET_MARKER)

    monkeypatch.setattr(health_router_module, "get_connection", _fake_db_connection())
    monkeypatch.setattr(admin_service_module, "health_runs", _raise)

    app = create_app()
    app.dependency_overrides[get_current_user] = lambda: ADMIN
    with TestClient(app) as test_client:
        response = test_client.get("/health/runs")

    assert response.status_code == 500
    assert response.json() == {"error": "internal_error"}
    assert _SECRET_MARKER not in response.text
    assert "RuntimeError" not in response.text
    assert "Traceback" not in response.text


@pytest.fixture
def _production_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pass@localhost/db")
    monkeypatch.setenv("SECRET_KEY", "test-secret")
    monkeypatch.setenv("GROQ_API_KEY", "test-key")
    monkeypatch.setenv("CORS_ALLOW_ORIGINS", "https://frontend.example.com")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_unmodeled_exception_returns_generic_500_in_production_too(
    _production_env, monkeypatch: pytest.MonkeyPatch
):
    import app.api.routers.health as health_router_module
    import app.services.admin as admin_service_module

    def _raise(conn, limit):
        raise RuntimeError(_SECRET_MARKER)

    monkeypatch.setattr(health_router_module, "get_connection", _fake_db_connection())
    monkeypatch.setattr(admin_service_module, "health_runs", _raise)

    app = create_app()
    app.dependency_overrides[get_current_user] = lambda: ADMIN
    with TestClient(app) as test_client:
        response = test_client.get("/health/runs")

    assert response.status_code == 500
    assert response.json() == {"error": "internal_error"}
    assert _SECRET_MARKER not in response.text
    assert "RuntimeError" not in response.text


def test_debug_is_explicitly_false(client):
    # FastAPI's `debug` flag is what would otherwise render a full
    # traceback as the 500 response body -- confirm it is off, not just
    # inferred from the tests above never having tripped it.
    app = create_app()
    assert app.debug is False
