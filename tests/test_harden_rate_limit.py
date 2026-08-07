"""app.middleware.rate_limit: general, whole-API rate limiting (HARDEN
mission item 3) -- distinct from and additional to app.services.auth's
OTP-specific per-email/per-IP limiter (RT-4/RT-5 remediation), which this
does not touch, weaken, or duplicate the request-counting logic of. Only
the IP-resolution algorithm is deliberately re-implemented here (see
app.middleware.rate_limit.resolve_client_ip's docstring for why it is a
duplicate, not an import, of app.api.routers.auth._client_ip's RT-5
logic), and this file re-proves that specific algorithm stays correct in
its own right.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from app.config import get_settings
from app.main import create_app
from app.middleware.rate_limit import resolve_client_ip


def _make_request(headers: dict[str, str], client_host: str = "10.0.0.1") -> Request:
    raw_headers = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
    scope = {
        "type": "http",
        "headers": raw_headers,
        "client": (client_host, 12345),
        "method": "GET",
        "path": "/",
        "query_string": b"",
    }
    return Request(scope)


class TestResolveClientIp:
    def test_default_zero_hops_ignores_x_forwarded_for(self):
        # RT-5's fail-safe default: hops=0 never even looks at the header.
        request = _make_request({"x-forwarded-for": "1.2.3.4"}, client_host="10.0.0.1")
        assert resolve_client_ip(request, trusted_proxy_hops=0) == "10.0.0.1"

    def test_spoofed_leftmost_entry_does_not_win_with_one_trusted_hop(self):
        # Render (1 hop) appends the real client as the last entry;
        # everything left of that is attacker-controlled.
        request = _make_request({"x-forwarded-for": "attacker-supplied, 203.0.113.9"})
        assert resolve_client_ip(request, trusted_proxy_hops=1) == "203.0.113.9"

    def test_falls_back_to_socket_peer_when_header_absent(self):
        request = _make_request({}, client_host="10.0.0.5")
        assert resolve_client_ip(request, trusted_proxy_hops=1) == "10.0.0.5"


@pytest.fixture
def _tight_rate_limit_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("RATE_LIMIT_REQUESTS_PER_WINDOW", "3")
    monkeypatch.setenv("RATE_LIMIT_WINDOW_SECONDS", "60")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_requests_beyond_the_window_get_429(_tight_rate_limit_env):
    with TestClient(create_app()) as test_client:
        statuses = [test_client.get("/keywords").status_code for _ in range(4)]
    # First 3 pass the limiter through to the route (401, unauthenticated
    # -- proves the limiter isn't what's producing the earlier statuses).
    assert statuses[:3] == [401, 401, 401]
    assert statuses[3] == 429


def test_429_response_has_retry_after_and_uniform_envelope(_tight_rate_limit_env):
    with TestClient(create_app()) as test_client:
        for _ in range(3):
            test_client.get("/keywords")
        response = test_client.get("/keywords")
    assert response.status_code == 429
    assert response.json()["error"] == "rate_limited"
    assert int(response.headers["retry-after"]) > 0


def test_healthz_is_exempt_from_the_general_limit(_tight_rate_limit_env):
    with TestClient(create_app()) as test_client:
        statuses = [test_client.get("/healthz").status_code for _ in range(20)]
    assert all(status == 200 for status in statuses)
