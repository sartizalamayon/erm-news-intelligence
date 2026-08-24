"""app.middleware.security_headers: every response carries the baseline
hardening headers; HSTS is environment-gated; the CSP is not sent on the
docs paths it would otherwise break (see that module's docstring for the
full reasoning).
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import create_app


def test_baseline_headers_present_on_every_response(client):
    response = client.get("/healthz")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert "permissions-policy" in response.headers
    assert response.headers["content-security-policy"] == "default-src 'none'; frame-ancestors 'none'; base-uri 'none'"


def test_headers_present_on_error_responses_too(client):
    # A 404 goes through the same middleware stack as a 200 -- headers
    # must not depend on the route handler having run.
    response = client.get("/this-route-does-not-exist")
    assert response.status_code == 404
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "content-security-policy" in response.headers


def test_hsts_absent_outside_production_like_env(client):
    # tests/conftest.py forces APP_ENV=test.
    response = client.get("/healthz")
    assert "strict-transport-security" not in response.headers




def test_hsts_present_in_production(production_env):
    with TestClient(create_app()) as prod_client:
        response = prod_client.get("/healthz")
    assert response.headers["strict-transport-security"] == "max-age=63072000; includeSubDomains"


def test_docs_disabled_in_production(production_env):
    with TestClient(create_app()) as prod_client:
        assert prod_client.get("/docs").status_code == 404
        assert prod_client.get("/redoc").status_code == 404
        assert prod_client.get("/openapi.json").status_code == 404


def test_docs_enabled_outside_production_and_csp_exempted(client):
    # tests/conftest.py's `client` fixture is APP_ENV=test -> docs stay on.
    response = client.get("/docs")
    assert response.status_code == 200
    # The strict API CSP would break Swagger UI's CDN assets -- confirm
    # it is deliberately not sent on this specific path.
    assert "content-security-policy" not in response.headers
    # Other baseline headers are still present -- only CSP is exempted.
    assert response.headers["x-content-type-options"] == "nosniff"
