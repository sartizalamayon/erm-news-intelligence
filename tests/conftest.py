"""Shared pytest fixtures.

Sets safe, non-production env vars before anything imports app.config,
so `get_settings()` never accidentally picks up a real developer's local
`.env`. CI must never call a real LLM provider or the live internet
(mission constraint) — the blank provider keys enforce that by
construction: any code path that tried a real call would fail fast on
an empty key rather than silently succeeding against production.

Extended by ARCH-TEST (see docs/test-strategy.md) with two more
structural guards, both explained in full where they're implemented:

  * A process-wide network-socket guard (tests/helpers/network_guard.py),
    autouse for every test — mission §7's "a P2 or P3 test that tries to
    open a socket should fail loudly" as an actual mechanism, not a
    convention. Opt out per-test with `@pytest.mark.allow_network`.
  * A `parity` / `budget_lab` marker pair that pytest SKIPS by default —
    mission §6.4's "excluded by default that CI never selects", enforced
    via pytest_collection_modifyitems below rather than a pyproject.toml
    addopts entry (ARCH-TEST does not own pyproject.toml). Pass
    `--run-parity` to include them.

Neither addition touches or removes ARCH-REPO's original `client`
fixture or the env-var setdefaults above.
"""

from __future__ import annotations

import os

import pytest

from tests.helpers.network_guard import block_network, restore_network

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("LOG_LEVEL", "WARNING")
os.environ.setdefault("DATABASE_URL", "")
os.environ.setdefault("GROQ_API_KEY", "")
os.environ.setdefault("GEMINI_API_KEY", "")
os.environ.setdefault("SECRET_KEY", "")


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    from app.main import create_app

    with TestClient(create_app()) as test_client:
        yield test_client


# ── Network guard (mission §7) ──────────────────────────────────────────


@pytest.fixture(autouse=True)
def _network_guard(request: pytest.FixtureRequest):
    """Blocks every real outbound socket connection for the duration of
    each test, unless the test is marked `@pytest.mark.allow_network`.
    See tests/helpers/network_guard.py for what is/isn't affected (the
    `client` fixture's in-process ASGI TestClient is NOT affected — no
    real socket involved).
    """
    if request.node.get_closest_marker("allow_network"):
        yield
        return
    block_network()
    try:
        yield
    finally:
        restore_network()


# ── On-demand-only markers (mission §6.4 cost discipline) ──────────────


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "parity: the P3 parity harness's end-to-end checks against the frozen "
        "sample. Excluded by default (see pytest_collection_modifyitems below) "
        "— run explicitly with `pytest -m parity --run-parity`. Building the "
        "harness does not, by itself, spend LLM tokens; a REAL run against a "
        "live provider is a separate, human-initiated action this repo's test "
        "suite never performs on its own.",
    )
    config.addinivalue_line(
        "markers",
        "budget_lab: on-demand token-budget experiments (mission §6.4). "
        "Excluded by default alongside `parity`; none exist in this repo yet "
        "— the marker is registered so a future one is excluded by construction, "
        "not by remembering to ask.",
    )
    config.addinivalue_line(
        "markers",
        "allow_network: opt this specific test out of the autouse network guard. "
        "No test in this repo uses it today; document WHY in the test's "
        "docstring if you ever add one.",
    )


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--run-parity",
        action="store_true",
        default=False,
        help="Include tests marked `parity` or `budget_lab` (skipped by default).",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if config.getoption("--run-parity"):
        return
    skip = pytest.mark.skip(reason="on-demand only — pass --run-parity to include (mission §6.4)")
    for item in items:
        if item.get_closest_marker("parity") or item.get_closest_marker("budget_lab"):
            item.add_marker(skip)
