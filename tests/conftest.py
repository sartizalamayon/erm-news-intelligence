"""Shared pytest fixtures.

Sets safe, non-production env vars before anything imports app.config,
so `get_settings()` never accidentally picks up a real developer's local
`.env`. CI must never call a real LLM provider or the live internet
(mission constraint) — the blank provider keys enforce that by
construction: any code path that tried a real call would fail fast on
an empty key rather than silently succeeding against production.
"""

from __future__ import annotations

import os

import pytest

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
