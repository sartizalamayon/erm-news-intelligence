"""app.middleware.request_limits: request body size cap and generic
query-string / pagination bounds (HARDEN mission item 4).

These are enforced at the middleware layer, in front of every route,
because app/api/routers/** and app/api/schemas/** are out of scope for
this mission -- see that module's docstring. Two concrete, previously
real gaps this closes:

  * GET /health/runs?limit=<n> (app/api/routers/health.py) has no upper
    bound in its own signature (`limit: int = 5`) -- an unbounded value
    now gets rejected before the route ever runs.
  * GET /search (app/api/routers/search.py) does not exist yet as a
    route, so there is nothing to bound today -- but the query-param
    length cap already applies process-wide, so whenever a `q=`
    parameter is added there, it inherits the bound with no further
    change needed here.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import create_app


def test_query_param_over_length_limit_is_rejected(client):
    huge_value = "x" * 3000
    response = client.get("/keywords", params={"section": huge_value})
    assert response.status_code == 400
    assert response.json()["error"] == "validation_error"


def test_query_param_within_length_limit_reaches_the_route(client):
    # 401 (unauthenticated), not 400 -- proves this passed the bounds
    # check and reached require_admin.
    response = client.get("/keywords", params={"section": "sports"})
    assert response.status_code == 401


@pytest.mark.parametrize("limit_value", ["999999999", "0", "-5"])
def test_unbounded_or_invalid_pagination_limit_is_rejected(client, limit_value):
    response = client.get("/health/runs", params={"limit": limit_value})
    assert response.status_code == 400
    assert "limit" in response.json()["detail"]


def test_non_integer_pagination_limit_is_rejected(client):
    response = client.get("/health/runs", params={"limit": "not-a-number"})
    assert response.status_code == 400


def test_reasonable_pagination_limit_reaches_the_route(client):
    response = client.get("/health/runs", params={"limit": "5"})
    assert response.status_code == 401  # unauthenticated, but past the bounds check


def test_negative_offset_is_rejected(client):
    # "offset" is checked generically by key name, regardless of whether
    # the route on the receiving end actually declares one -- GET
    # /search (CORE-API) uses `page`, not `offset`, but the bounds check
    # runs in front of every route and doesn't know or care.
    response = client.get("/search", params={"offset": "-1"})
    assert response.status_code == 400


def test_zero_offset_is_allowed_through(client):
    # GET /search now exists (CORE-API) and requires an authenticated
    # session -- 401, not 400, proves offset=0 passed the bounds
    # middleware and only then hit the real route.
    response = client.get("/search", params={"offset": "0"})
    assert response.status_code == 401


def test_a_large_but_realistic_offset_reaches_the_route(client):
    """The bug this test exists for: `offset` used to share `limit`'s
    10,000 ceiling, so paging deep into a real corpus (the Review queue
    Load More sends `offset=<articles already loaded>`, and a real
    corpus comfortably exceeds 10,000 pending rows) 400'd for a reason
    that had nothing to do with anything being wrong. `offset` now has
    its own, much higher ceiling -- 15,000 must reach the route (401,
    not 400).
    """
    response = client.get("/search", params={"offset": "15000"})
    assert response.status_code == 401


def test_a_pathological_offset_is_still_rejected(client):
    # offset's raised ceiling is high, not unlimited -- still bounded
    # well above any realistic corpus size.
    response = client.get("/search", params={"offset": "999999999"})
    assert response.status_code == 400
    assert "offset" in response.json()["detail"]


def test_search_free_text_over_length_limit_is_rejected(client):
    # The mission-named case: GET /search's free-text `q=` hits a
    # GIN-indexed column with no length cap of its own (`q: str = ""`,
    # a plain function param, not schema-validated) -- this middleware
    # is what actually bounds it.
    response = client.get("/search", params={"q": "x" * 3000})
    assert response.status_code == 400
    assert response.json()["error"] == "validation_error"


def test_search_reasonable_query_reaches_the_route(client):
    response = client.get("/search", params={"q": "election"})
    assert response.status_code == 401  # unauthenticated, but past the bounds check


@pytest.fixture
def _tiny_body_limit_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MAX_REQUEST_BODY_BYTES", "64")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_body_over_the_configured_limit_is_rejected_413(_tiny_body_limit_env):
    big_body = ("{" + '"email": "' + "a" * 200 + '@grameenphone.com"}').encode()
    with TestClient(create_app()) as test_client:
        response = test_client.post(
            "/auth/otp/request",
            content=big_body,
            headers={"Content-Type": "application/json"},
        )
    assert response.status_code == 413
    assert response.json()["error"] == "validation_error"


def test_body_within_the_limit_reaches_normal_validation(_tiny_body_limit_env):
    small_body = b'{"email": "a@x.com"}'
    with TestClient(create_app()) as test_client:
        response = test_client.post(
            "/auth/otp/request",
            content=small_body,
            headers={"Content-Type": "application/json"},
        )
    # Not 413 -- reached the real handler and failed for an unrelated
    # reason (no DATABASE_URL configured in the test env), proving the
    # body size cap itself let a small request through.
    assert response.status_code != 413
