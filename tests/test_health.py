"""Smoke test: the app boots and its liveness probe responds. Keeps CI
green from day one (DoD #5) with something real, not a trivial `assert True`.
"""

from __future__ import annotations


def test_healthz_ok(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
