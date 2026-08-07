"""Authorization tests for the admin console API (ADMIN-API mission).

DoD #7: "every admin route must be proven to reject a non-admin session
and an unauthenticated request. This is the most important test class
you write." Every route added by app/api/routers/{sources,keywords,
prompts,dashboard,health}.py is enumerated below and hit twice: once
with no session cookie at all (must be 401, via
`app.api.deps.get_current_user`), once with a valid ANALYST session
(must be 403, via `app.api.deps.require_admin`) -- proving role
enforcement happens at the ROUTE layer, independent of anything the
frontend does, exactly like tests/test_auth_api.py's identical proof
for GET /auth/otp-failures.

No real database is touched by any test in this file: overriding
`get_current_user` intercepts the auth dependency chain before
`require_admin`'s route-layer check runs, and a 401/403 response is
returned before the route's own handler body (which opens a DB
connection) ever executes -- see app.api.deps.require_admin, which
raises before control ever reaches the endpoint function.

Bodies are minimal-but-VALID for every write route, deliberately, so a
malformed-body 422 can never be mistaken for the 401/403 this file
exists to prove -- whichever way a given FastAPI version orders
dependency-resolution vs. body-validation, the body here always
validates, so the only variable under test is authentication/role.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.api.deps import AuthenticatedUser, get_current_user
from app.main import create_app

ANALYST = AuthenticatedUser(
    id=1, email="analyst@grameenphone.com", role="analyst", display_name="Analyst", session_id=1
)

# (method, path, json_body) -- every route the 5 routers this task owns
# add. Keep in sync by hand with app/api/routers/{sources,keywords,
# prompts,dashboard,health}.py whenever a route is added/removed.
ADMIN_ROUTES: list[tuple[str, str, dict | None]] = [
    # ── sources.py ──
    ("GET", "/sources", None),
    ("POST", "/sources", {"name": "Test Source", "methods": ["rss"]}),
    ("PATCH", "/sources/1", {"enabled": True}),
    ("DELETE", "/sources/1", None),
    ("POST", "/sources/1/toggle", None),
    ("POST", "/sources/1/test", None),
    ("POST", "/sources/1/diagnose", None),
    # ── keywords.py ──
    ("GET", "/keywords", None),
    ("GET", "/keywords/sections", None),
    ("POST", "/keywords", {"section": "test", "term_en": "x"}),
    ("PATCH", "/keywords/1", {"enabled": True}),
    ("DELETE", "/keywords/1", None),
    ("POST", "/keywords/1/toggle", None),
    ("POST", "/keywords/rematch", None),
    # ── prompts.py (also hosts provider routing/quota config) ──
    ("GET", "/prompts/config", None),
    ("PUT", "/prompts/guidance", {"content": "x"}),
    ("PUT", "/prompts/examples", {"content": "x"}),
    ("GET", "/prompts/guidance/versions", None),
    ("GET", "/prompts/examples/versions", None),
    ("POST", "/prompts/guidance/versions/1/activate", None),
    ("POST", "/prompts/examples/versions/1/activate", None),
    ("GET", "/prompts/candidates", None),
    ("POST", "/prompts/suggest", None),
    ("GET", "/prompts/routing", None),
    ("PUT", "/prompts/routing", {"routes": {}}),
    ("GET", "/prompts/providers", None),
    ("PATCH", "/prompts/providers/groq", {"role": "primary"}),
    ("GET", "/prompts/quota", None),
    ("PUT", "/prompts/quota/groq", {"tpd": 1000}),
    ("GET", "/prompts/provider-definitions", None),
    (
        "PUT",
        "/prompts/provider-definitions/mistral",
        {
            "adapter": "openai_compatible",
            "base_url": "https://api.mistral.ai/v1",
            "default_model": "mistral-small-latest",
        },
    ),
    ("GET", "/prompts/provider-definitions/mistral/credentials", None),
    ("POST", "/prompts/provider-definitions/mistral/credentials", {"key_id": "primary", "api_key": "x"}),
    ("DELETE", "/prompts/provider-definitions/mistral/credentials/primary", None),
    # ── dashboard.py (storage dashboard) ──
    ("GET", "/dashboard/storage", None),
    ("POST", "/dashboard/storage/purge", {"operation": "stale_bodies", "confirm": False}),
    ("POST", "/dashboard/storage/export", {}),
    ("POST", "/dashboard/storage/import", {"path": "var/exports/x.jsonl.gz"}),
    # ── health.py ──
    ("GET", "/health/overview", None),
    ("GET", "/health/sources", None),
    ("GET", "/health/runs", None),
    ("POST", "/health/sources/1/test", None),
    ("POST", "/health/sources/1/diagnose", None),
    ("POST", "/health/pipeline/stages/translate/run", None),
    ("POST", "/health/pipeline/run-all", None),
]


_ROUTE_IDS = [f"{method}_{path}" for method, path, _ in ADMIN_ROUTES]


@pytest.mark.parametrize("route", ADMIN_ROUTES, ids=_ROUTE_IDS)
def test_admin_route_rejects_unauthenticated_request(client, route: tuple[str, str, dict | None]) -> None:
    method, path, body = route
    resp = client.request(method, path, json=body)
    assert resp.status_code == 401, f"{method} {path} -> {resp.status_code}, expected 401 (no session cookie)"


@pytest.mark.parametrize("route", ADMIN_ROUTES, ids=_ROUTE_IDS)
def test_admin_route_rejects_non_admin_session(route: tuple[str, str, dict | None]) -> None:
    method, path, body = route
    app = create_app()
    app.dependency_overrides[get_current_user] = lambda: ANALYST
    with TestClient(app) as test_client:
        resp = test_client.request(method, path, json=body)
    assert resp.status_code == 403, f"{method} {path} -> {resp.status_code}, expected 403 (analyst session)"


def test_admin_routes_table_is_non_trivial() -> None:
    """Sanity guard: fails loudly if ADMIN_ROUTES was ever accidentally
    emptied, rather than the parametrized tests above silently
    collecting zero cases.
    """
    assert len(ADMIN_ROUTES) >= 35
    assert len({(m, p) for m, p, _ in ADMIN_ROUTES}) == len(ADMIN_ROUTES), "duplicate (method, path) entries"
