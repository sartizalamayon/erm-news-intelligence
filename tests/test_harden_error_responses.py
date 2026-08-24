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
    body = response.json()
    assert body["error"] == "validation_error"
    # A malformed body has no field to name, so it is labelled "body"
    # rather than pydantic's raw byte offset (which rendered as a field
    # literally called "1").
    assert body["fields"] == [{"field": "body", "problem": body["fields"][0]["problem"]}]
    assert body["detail"].startswith("body: ")


def test_malformed_json_does_not_echo_the_submitted_body(client):
    weird_input = "not-json-but-contains-a-marker-9f3c1a"
    response = client.post(
        "/auth/otp/request",
        content=weird_input.encode(),
        headers={"Content-Type": "application/json"},
    )
    assert weird_input not in response.text


def test_wrong_field_type_returns_uniform_422_envelope(client):
    # code must be a string per app.api.schemas.auth.SetPasswordBody --
    # sending an int fails pydantic validation (RequestValidationError),
    # not app.errors.ValidationError.
    response = client.post(
        "/auth/password/set",
        json={"email": "a@grameenphone.com", "code": 123456, "new_password": "Zx9!Qwerty42Lmn"},
    )
    assert response.status_code == 422
    body = response.json()
    assert body["error"] == "validation_error"
    # The whole point of the change: name the offending field and say
    # what is wrong with it. A bare "The request body failed validation."
    # left an operator adding a source with no way to discover that
    # `methods` is a list, not a string.
    assert body["fields"] == [{"field": "code", "problem": "Input should be a valid string"}]
    assert body["detail"] == "code: Input should be a valid string"


def test_validation_error_names_the_field_but_never_echoes_its_value(client):
    """The security property the detailed 422 must not break.

    pydantic's `errors()` carries the submitted value under `input`.
    Field NAMES and pydantic's own message are safe to return; the
    submitted value is not — a bad OTP code or email would otherwise be
    reflected straight back to the caller (and into any log or proxy
    that records response bodies).
    """
    secret = "s3cret-value-must-not-be-reflected-4b1e7"
    response = client.post(
        "/auth/password/set",
        json={"email": "a@grameenphone.com", "code": [secret], "new_password": "Zx9!Qwerty42Lmn"},
    )
    assert response.status_code == 422
    assert secret not in response.text
    assert response.json()["fields"][0]["field"] == "code"


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
    # raise_server_exceptions=False: Starlette's ServerErrorMiddleware
    # (which owns the Exception/500 handler app.api.error_handlers
    # registers) sends the client a clean response AND THEN
    # deliberately re-raises the original exception -- documented
    # Starlette behavior, "allows test clients to optionally raise the
    # error within the test case." TestClient's default is to honor
    # that re-raise (useful for catching bugs elsewhere); here the
    # thing under test IS "what does the client actually receive", so
    # that re-raise is turned off to inspect the real response instead
    # of treating our own deliberately-raised RuntimeError as a pytest
    # failure.
    with TestClient(app, raise_server_exceptions=False) as test_client:
        response = test_client.get("/health/runs")

    assert response.status_code == 500
    assert response.json() == {"error": "internal_error"}
    assert _SECRET_MARKER not in response.text
    assert "RuntimeError" not in response.text
    assert "Traceback" not in response.text




def test_unmodeled_exception_returns_generic_500_in_production_too(
    production_env, monkeypatch: pytest.MonkeyPatch
):
    import app.api.routers.health as health_router_module
    import app.services.admin as admin_service_module

    def _raise(conn, limit):
        raise RuntimeError(_SECRET_MARKER)

    monkeypatch.setattr(health_router_module, "get_connection", _fake_db_connection())
    monkeypatch.setattr(admin_service_module, "health_runs", _raise)

    app = create_app()
    app.dependency_overrides[get_current_user] = lambda: ADMIN
    # raise_server_exceptions=False: Starlette's ServerErrorMiddleware
    # (which owns the Exception/500 handler app.api.error_handlers
    # registers) sends the client a clean response AND THEN
    # deliberately re-raises the original exception -- documented
    # Starlette behavior, "allows test clients to optionally raise the
    # error within the test case." TestClient's default is to honor
    # that re-raise (useful for catching bugs elsewhere); here the
    # thing under test IS "what does the client actually receive", so
    # that re-raise is turned off to inspect the real response instead
    # of treating our own deliberately-raised RuntimeError as a pytest
    # failure.
    with TestClient(app, raise_server_exceptions=False) as test_client:
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
