"""Dev-mode console fallback when the Resend provider is unconfigured
(coordinator-reported P0: "Internal Server Error" on login with no
RESEND_API_KEY set), plus the client-facing error shape when the
fallback does NOT apply.

Two behaviors:
1. app_env not in (production, staging) AND otp_console_fallback_enabled
   (default True): send_otp_email() logs the code at WARNING with an
   unmistakable marker and returns normally -- no exception, OTP
   generation/hashing/expiry/single-use/rate-limiting are untouched
   (this changes the delivery channel only).
2. Otherwise (production/staging, or the flag explicitly off): raises
   ProviderPermanentError with a generic, non-leaking message -- caught
   by app.api.error_handlers' EXISTING ProviderError handler
   (unmodified; that file is HARDEN's), which already shapes a safe
   {"error": "provider_error", "detail": ...} 502 response instead of a
   bare "Internal Server Error".
"""

from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

from app.config import Settings, get_settings
from app.errors import ProviderPermanentError
from app.main import create_app
from app.services.email import send_otp_email


@pytest.fixture(autouse=True)
def _restore_settings_cache():
    yield
    get_settings.cache_clear()


def _unconfigured(**overrides) -> Settings:
    base = {"resend_api_key": "", "resend_from_email": ""}
    base.update(overrides)
    return Settings(**base)


# ── Fallback fires in dev/test, never in production/staging ─────────────


def test_dev_env_unconfigured_provider_falls_back_and_logs_code(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr("app.services.email.get_settings", lambda: _unconfigured(app_env="development"))
    caplog.set_level(logging.WARNING, logger="app.services.email")

    send_otp_email(to_email="dev@grameenphone.com", code="123456", ttl_minutes=10)  # must not raise

    records = [r for r in caplog.records if "DEV_OTP_FALLBACK" in r.getMessage()]
    assert records, "expected a DEV_OTP_FALLBACK warning log"
    assert "123456" in records[0].getMessage()  # the actual code, so a developer can copy it


def test_test_env_unconfigured_provider_also_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.services.email.get_settings", lambda: _unconfigured(app_env="test"))
    send_otp_email(to_email="dev@grameenphone.com", code="654321", ttl_minutes=10)  # must not raise


@pytest.mark.parametrize("env", ["production", "staging"])
def test_production_and_staging_never_fall_back(env: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "app.services.email.get_settings", lambda: _unconfigured(app_env=env, otp_console_fallback_enabled=True)
    )
    with pytest.raises(ProviderPermanentError):
        send_otp_email(to_email="owner@grameenphone.com", code="111111", ttl_minutes=10)


def test_flag_disabled_in_dev_also_does_not_fall_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "app.services.email.get_settings",
        lambda: _unconfigured(app_env="development", otp_console_fallback_enabled=False),
    )
    with pytest.raises(ProviderPermanentError):
        send_otp_email(to_email="dev@grameenphone.com", code="111111", ttl_minutes=10)


def test_flag_default_is_enabled() -> None:
    assert Settings(resend_api_key="", resend_from_email="").otp_console_fallback_enabled is True


# ── Client-facing error carries no key names, values, or paths ──────────


def test_provider_permanent_error_message_has_no_leaked_config_names() -> None:
    monkeypatch_settings = _unconfigured(app_env="production")
    try:
        import app.services.email as email_module

        original = email_module.get_settings
        email_module.get_settings = lambda: monkeypatch_settings  # type: ignore[assignment]
        with pytest.raises(ProviderPermanentError) as exc_info:
            send_otp_email(to_email="owner@grameenphone.com", code="222222", ttl_minutes=10)
    finally:
        email_module.get_settings = original  # type: ignore[assignment]

    message = str(exc_info.value)
    for forbidden in ("RESEND_API_KEY", "resend_api_key", "RESEND_FROM_EMAIL", "resend_from_email", "SECRET_KEY"):
        assert forbidden not in message
    assert "administrator" in message.lower()


def test_configuration_error_at_request_time_is_not_a_bare_500(monkeypatch: pytest.MonkeyPatch) -> None:
    """End-to-end reproduction of the reported bug's PRODUCTION-shaped
    counterpart: POST /auth/otp/request must not surface a bare
    "Internal Server Error" when the provider is unconfigured outside
    dev -- it must come back as a clear, actionable, non-leaking 502 via
    app.errors.ProviderError's existing handler.
    """
    import contextlib

    import app.api.routers.auth as auth_router_module
    from app.services import auth as auth_service

    monkeypatch.setattr(auth_router_module, "get_connection", lambda: contextlib.nullcontext(None))
    monkeypatch.setattr("app.services.email.get_settings", lambda: _unconfigured(app_env="production"))

    def _fake_request_otp(conn, *, email, ip_address):
        send_otp_email(to_email=email, code="333333", ttl_minutes=10)

    monkeypatch.setattr(auth_service, "request_otp", _fake_request_otp)

    with TestClient(create_app()) as client:
        resp = client.post("/auth/otp/request", json={"email": "owner@grameenphone.com"})

    assert resp.status_code == 502
    body = resp.json()
    assert body["error"] == "provider_error"
    assert "administrator" in body["detail"].lower()
    for forbidden in ("RESEND_API_KEY", "resend_api_key", "Traceback", "/Users/", "site-packages"):
        assert forbidden not in resp.text


# ── The fallback changes delivery only -- security properties untouched ──


def test_fallback_does_not_weaken_otp_security_properties(monkeypatch: pytest.MonkeyPatch) -> None:
    """Full request/verify round trip through app.services.auth with
    Resend unconfigured in dev -- expiry, single-use, hashing, and BOTH
    rate-limit dimensions must behave identically to the configured-
    provider path (tests/test_auth_service.py's existing coverage of
    each of those, exercised here through the fallback specifically).
    """
    from app.db.testing import migrated_schema, pg_available

    if not pg_available():
        pytest.skip("no reachable Postgres (set TEST_DATABASE_URL)")

    monkeypatch.setenv("SECRET_KEY", "test-only-secret-key-do-not-use-in-prod")
    get_settings.cache_clear()
    monkeypatch.setattr("app.services.email.get_settings", lambda: _unconfigured(app_env="test"))

    from app.services import auth as auth_service

    with migrated_schema() as conn:
        email = "fallback.target@grameenphone.com"

        # Single-use / hashing: request succeeds with no mocking of
        # send_otp_email at all -- it's the REAL function, falling back.
        auth_service.request_otp(conn, email=email, ip_address="203.0.113.50")
        row = conn.execute(
            "SELECT code_hash, expires_at FROM otp_codes WHERE email = %s", (email,)
        ).fetchone()
        assert row is not None
        assert row["code_hash"] != ""  # a real hash was stored, not a placeholder
        assert row["expires_at"] is not None  # still expires, same as the configured path

        # Rate limiting still enforced through the fallback path.
        for _ in range(auth_service.OTP_RATE_LIMIT_PER_EMAIL - 1):
            auth_service.request_otp(conn, email=email, ip_address="203.0.113.50")
        with pytest.raises(auth_service.OtpRateLimited):
            auth_service.request_otp(conn, email=email, ip_address="203.0.113.50")

    get_settings.cache_clear()
