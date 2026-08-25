"""Render's health check must not be answerable with a 429.

`app.middleware.rate_limit._EXEMPT_PATHS` contained exactly one entry,
`/healthz` -- the liveness route declared in `app.main`. But render.yaml's
`healthCheckPath` is `/health/ready`, the readiness route in
`app.api.routers.health`, and that one was NOT exempt. The exemption set
named the probe nobody had configured and omitted the probe the platform
actually calls.

Why that matters even though 120 req/min is far above any health
checker's own poll rate: the limiter keys on the RESOLVED client IP, and
Render's checker is not guaranteed to resolve to an address of its own.
Share a bucket with live traffic -- a burst, a scraper, one enthusiastic
user -- and the probe gets a 429. Render reads any non-2xx as unhealthy
and restarts the instance; the restart wipes the in-process counters, so
by the time anyone looks there is nothing left to explain why. A rate
limiter must never be in a position to take the platform's own view of
"is this service alive" hostage.

The first test below is the drift guard, and it is the one that matters
most: it reads render.yaml's REAL `healthCheckPath` rather than the
string "/health/ready", so changing the blueprint without changing the
middleware fails here. The middleware and the blueprint are edited by
different people at different times and nothing else connects them --
which is precisely how they came to disagree.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.middleware.rate_limit import _EXEMPT_PATHS, RateLimitMiddleware

_REPO_ROOT = Path(__file__).resolve().parents[1]


def _render_blueprint() -> dict:
    return yaml.safe_load((_REPO_ROOT / "render.yaml").read_text(encoding="utf-8"))


def _configured_health_check_paths() -> list[str]:
    """Every `healthCheckPath` any service in render.yaml declares."""
    services = _render_blueprint().get("services") or []
    return [s["healthCheckPath"] for s in services if s.get("healthCheckPath")]


def test_render_declares_a_health_check_path_at_all():
    """Guards the guard below: if render.yaml ever stops declaring one,
    the drift test would pass vacuously by iterating an empty list.
    """
    paths = _configured_health_check_paths()
    assert paths, "render.yaml declares no healthCheckPath -- the test below would be vacuous"


def test_every_configured_health_check_path_is_rate_limit_exempt():
    """THE regression. Before the fix `healthCheckPath: /health/ready`
    was not in `_EXEMPT_PATHS`, and this fails.
    """
    for path in _configured_health_check_paths():
        assert path in _EXEMPT_PATHS, (
            f"render.yaml points Render's health checker at {path}, but "
            "app.middleware.rate_limit does not exempt it -- a traffic "
            "burst could answer the health check with a 429 and get the "
            "instance restarted."
        )


def test_liveness_probe_stays_exempt_too():
    """`/healthz` was the original entry and must not be lost while
    adding the readiness path. It is polled by anything watching the
    process and is deliberately dependency-free.
    """
    assert "/healthz" in _EXEMPT_PATHS


@pytest.fixture
def one_request_per_window_app() -> FastAPI:
    """A limiter tightened to a single request per window, so exemption
    is observable in three calls instead of a hundred and twenty-one.
    Built here rather than reusing the app fixture because the real
    limit (120/min) cannot be exhausted in a test that stays fast.
    """
    app = FastAPI()

    @app.get("/health/ready")
    def ready() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/not-a-probe")
    def other() -> dict[str, str]:
        return {"status": "ok"}

    app.add_middleware(RateLimitMiddleware, requests=1, window_seconds=60, trusted_proxy_hops=0)
    return app


def test_readiness_probe_is_never_rate_limited_in_practice(one_request_per_window_app):
    """The behavioural half: not "the string is in a set" but "repeated
    polling from one address keeps getting 200". This is what Render
    does, several times a minute, forever.
    """
    with TestClient(one_request_per_window_app) as client:
        for _ in range(5):
            assert client.get("/health/ready").status_code == 200


def test_a_non_probe_path_is_still_rate_limited(one_request_per_window_app):
    """The other half, and the reason the exemption has to be a list of
    paths rather than a blanket softening: exempting the probe must not
    exempt anything else.
    """
    with TestClient(one_request_per_window_app) as client:
        assert client.get("/not-a-probe").status_code == 200
        second = client.get("/not-a-probe")
        assert second.status_code == 429
        assert second.headers.get("Retry-After")
