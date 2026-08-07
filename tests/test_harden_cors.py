"""CORS policy (HARDEN mission item 1): the frontend origin is
allowlisted from config, credentials are allowed (the session cookie is
cross-subdomain, SameSite=None; Secure per ADR §1), and a wildcard origin
combined with credentials -- invalid per the CORS spec and a real
vulnerability -- is rejected outright at startup rather than silently
misconfigured.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import Settings, get_settings
from app.errors import ConfigurationError
from app.main import _validate_cors_settings, create_app


def test_wildcard_origin_is_rejected():
    settings = Settings(cors_allow_origins="*", _env_file=None)  # type: ignore[call-arg]
    with pytest.raises(ConfigurationError, match="must not contain"):
        _validate_cors_settings(settings)


def test_wildcard_among_multiple_origins_is_also_rejected():
    settings = Settings(cors_allow_origins="https://a.example, *", _env_file=None)  # type: ignore[call-arg]
    with pytest.raises(ConfigurationError):
        _validate_cors_settings(settings)


def test_empty_origins_in_production_is_rejected():
    settings = Settings(
        app_env="production",
        database_url="postgresql://x",
        secret_key="y",
        groq_api_key="z",
        cors_allow_origins="",
        _env_file=None,  # type: ignore[call-arg]
    )
    with pytest.raises(ConfigurationError, match="empty"):
        _validate_cors_settings(settings)


def test_empty_origins_outside_production_is_fine():
    settings = Settings(app_env="development", cors_allow_origins="", _env_file=None)  # type: ignore[call-arg]
    _validate_cors_settings(settings)  # should not raise


def test_valid_single_origin_passes():
    settings = Settings(
        app_env="production",
        database_url="postgresql://x",
        secret_key="y",
        groq_api_key="z",
        cors_allow_origins="https://erm-frontend.onrender.com",
        _env_file=None,  # type: ignore[call-arg]
    )
    _validate_cors_settings(settings)  # should not raise


@pytest.fixture
def _configured_origin_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CORS_ALLOW_ORIGINS", "https://erm-frontend.onrender.com")
    get_settings.cache_clear()
    yield "https://erm-frontend.onrender.com"
    get_settings.cache_clear()


def test_allowed_origin_gets_credentialed_cors_headers(_configured_origin_env):
    origin = _configured_origin_env
    with TestClient(create_app()) as test_client:
        response = test_client.get("/healthz", headers={"Origin": origin})
    assert response.headers["access-control-allow-origin"] == origin
    assert response.headers["access-control-allow-credentials"] == "true"


def test_unlisted_origin_gets_no_cors_headers(_configured_origin_env):
    with TestClient(create_app()) as test_client:
        response = test_client.get("/healthz", headers={"Origin": "https://evil.example.com"})
    assert "access-control-allow-origin" not in response.headers


def test_no_origins_configured_means_no_cors_headers_at_all(client):
    # tests/conftest.py's `client` fixture has CORS_ALLOW_ORIGINS unset
    # -> app.main.create_app() never adds CORSMiddleware at all.
    response = client.get("/healthz", headers={"Origin": "https://anything.example.com"})
    assert "access-control-allow-origin" not in response.headers
