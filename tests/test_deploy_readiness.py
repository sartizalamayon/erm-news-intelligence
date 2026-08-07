"""Regression tests for GET /health/ready -- the Render readiness probe
added by the DEPLOY mission (render.yaml's healthCheckPath,
docs/deployment.md). Distinct from tests/test_health.py's existing
/healthz liveness smoke test (app.main, untouched by this task).
"""

from __future__ import annotations

import pytest

from app.config import Settings, get_settings
from app.db.testing import _candidate_dsn, pg_available


def test_ready_with_no_database_configured_is_ok(client):
    """The default test env (tests/conftest.py sets DATABASE_URL="") is a
    legitimate non-production state, not a readiness failure.
    """
    response = client.get("/health/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["database"] == "not_configured"


def test_ready_with_unreachable_database_returns_503(client):
    """A configured-but-unreachable DATABASE_URL (bad host/port, or a
    paused/down Supabase instance) must fail the probe with 503, not a
    false-positive 200 -- this is the whole point of the fix over the
    plain `{"status": "ok"}` `/healthz` had before.
    """
    bad_settings = Settings(database_url="postgresql://bad:bad@127.0.0.1:1/nope")
    client.app.dependency_overrides[get_settings] = lambda: bad_settings
    try:
        response = client.get("/health/ready")
        assert response.status_code == 503
    finally:
        client.app.dependency_overrides.pop(get_settings, None)


@pytest.mark.skipif(not pg_available(), reason="no reachable Postgres (set TEST_DATABASE_URL)")
def test_ready_with_reachable_database_returns_200(client):
    # Same DSN resolution app.db.testing.pg_available()'s own skipif
    # guard just used (TEST_DATABASE_URL -> DATABASE_URL -> the
    # postgresql:///erm_test local-peer-auth default) -- not just the
    # two env vars, so this test is consistent with whatever made
    # pg_available() return True in the first place.
    real_settings = Settings(database_url=_candidate_dsn())
    client.app.dependency_overrides[get_settings] = lambda: real_settings
    try:
        response = client.get("/health/ready")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok"
        assert body["database"] == "reachable"
    finally:
        client.app.dependency_overrides.pop(get_settings, None)


def test_ready_does_not_require_admin_auth(client):
    """Render's own health-checker cannot authenticate -- unlike every
    other route in app/api/routers/health.py, this one must be reachable
    with no session cookie at all (a bare, unauthenticated GET).
    """
    response = client.get("/health/ready")
    assert response.status_code != 401
    assert response.status_code != 403
