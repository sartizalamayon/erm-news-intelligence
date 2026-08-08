"""Provider-specific behavior of app.services.email -- Brevo (the
default, live-verified transport) and Resend (kept fully working as the
non-default alternative), both selected by
app.config.Settings.email_provider.

Provider-agnostic fallback/error-shape behavior (unconfigured provider,
console fallback, generic client-facing message) is covered by
tests/test_auth_email_fallback.py; this file is specifically about the
two transports' own wire shape (URL, headers, payload) and their
success/failure handling (DoD #9: "Brevo failure handling").
`requests.post` is monkeypatched directly in every test here -- the
network guard (tests/helpers/network_guard.py, autouse) would block a
real socket anyway, but these tests need deterministic control over the
provider's response shape (status code, body) that a blocked socket
alone can't simulate.
"""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import Mock

import pytest

from app.config import Settings
from app.errors import ProviderPermanentError, ProviderRateLimitedError
from app.services import email as email_module


class _FakeResponse:
    def __init__(self, status_code: int, json_body: dict[str, Any] | None = None, text: str = ""):
        self.status_code = status_code
        self._json_body = json_body if json_body is not None else {}
        self.text = text

    def json(self) -> dict[str, Any]:
        return self._json_body


def _brevo_settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "email_provider": "brevo",
        "brevo_api_key": "xkeysib-test-key",
        "brevo_from_email": "noshinarthi9@gmail.com",
        "brevo_from_name": "ERM",
        "resend_api_key": "",
        "resend_from_email": "",
    }
    base.update(overrides)
    return Settings(**base)


def _resend_settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "email_provider": "resend",
        "resend_api_key": "re_test_key",
        "resend_from_email": "otp@example.com",
        "brevo_api_key": "",
        "brevo_from_email": "",
    }
    base.update(overrides)
    return Settings(**base)


# ── Provider selection ────────────────────────────────────────────────────


def test_brevo_is_the_compiled_in_default() -> None:
    """The field's own default (not influenced by this machine's local
    .env, which explicitly sets EMAIL_PROVIDER=brevo anyway) -- see
    app.config.Settings.email_provider's description for why: Resend
    needs a verified sending domain grameenphone.com does not have and
    the owner has ruled out obtaining, Brevo was live-verified instead.
    """
    assert Settings.model_fields["email_provider"].default == "brevo"


def test_email_provider_switch_selects_brevo_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_post = Mock(return_value=_FakeResponse(201, {"messageId": "abc@smtp-relay.mailin.fr"}))
    monkeypatch.setattr(email_module.requests, "post", fake_post)
    monkeypatch.setattr(email_module, "get_settings", lambda: _brevo_settings())

    email_module.send_otp_email(to_email="person@grameenphone.com", code="123456", ttl_minutes=10)

    fake_post.assert_called_once()
    assert fake_post.call_args.args[0] == email_module.BREVO_API_URL


def test_email_provider_switch_selects_resend_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_post = Mock(return_value=_FakeResponse(200, {"id": "resend-message-id"}))
    monkeypatch.setattr(email_module.requests, "post", fake_post)
    monkeypatch.setattr(email_module, "get_settings", lambda: _resend_settings())

    email_module.send_otp_email(to_email="person@grameenphone.com", code="123456", ttl_minutes=10)

    fake_post.assert_called_once()
    assert fake_post.call_args.args[0] == email_module.RESEND_API_URL


# ── Brevo: wire shape (live-verified) ─────────────────────────────────────


def test_brevo_uses_api_key_header_not_authorization_bearer(monkeypatch: pytest.MonkeyPatch) -> None:
    """The exact header convention that was live-verified against Brevo's
    real API (two real emails delivered) -- 'api-key', NOT
    'Authorization: Bearer' (Resend's convention, which does NOT work
    against Brevo).
    """
    fake_post = Mock(return_value=_FakeResponse(201, {"messageId": "id@smtp-relay.mailin.fr"}))
    monkeypatch.setattr(email_module.requests, "post", fake_post)
    monkeypatch.setattr(email_module, "get_settings", lambda: _brevo_settings())

    email_module.send_otp_email(to_email="person@grameenphone.com", code="654321", ttl_minutes=10)

    headers = fake_post.call_args.kwargs["headers"]
    assert headers["api-key"] == "xkeysib-test-key"
    assert "Authorization" not in headers
    assert headers["Content-Type"] == "application/json"
    assert headers["accept"] == "application/json"


def test_brevo_payload_shape_matches_live_verified_body(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_post = Mock(return_value=_FakeResponse(201, {"messageId": "id@smtp-relay.mailin.fr"}))
    monkeypatch.setattr(email_module.requests, "post", fake_post)
    monkeypatch.setattr(email_module, "get_settings", lambda: _brevo_settings())

    email_module.send_otp_email(to_email="target@grameenphone.com", code="111222", ttl_minutes=10)

    payload = fake_post.call_args.kwargs["json"]
    assert payload["sender"] == {"name": "ERM", "email": "noshinarthi9@gmail.com"}
    assert payload["to"] == [{"email": "target@grameenphone.com"}]
    assert "111222" in payload["textContent"]
    assert payload["subject"]


def test_brevo_success_logs_the_returned_message_id(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="app.services.email")
    fake_post = Mock(return_value=_FakeResponse(201, {"messageId": "real-id@smtp-relay.mailin.fr"}))
    monkeypatch.setattr(email_module.requests, "post", fake_post)
    monkeypatch.setattr(email_module, "get_settings", lambda: _brevo_settings())

    email_module.send_otp_email(to_email="person@grameenphone.com", code="333444", ttl_minutes=10)

    matches = [r for r in caplog.records if "brevo_send_succeeded" in r.getMessage()]
    assert matches
    assert matches[0].message_id == "real-id@smtp-relay.mailin.fr"  # type: ignore[attr-defined]
    # The OTP code itself is never in a success log record.
    assert "333444" not in matches[0].getMessage()


# ── Brevo: failure handling (DoD #9) ──────────────────────────────────────


def test_brevo_429_raises_rate_limited_with_provider_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(email_module.requests, "post", Mock(return_value=_FakeResponse(429, text="quota exceeded")))
    monkeypatch.setattr(email_module, "get_settings", lambda: _brevo_settings())

    with pytest.raises(ProviderRateLimitedError) as exc_info:
        email_module.send_otp_email(to_email="person@grameenphone.com", code="555666", ttl_minutes=10)
    assert exc_info.value.provider == "brevo"
    assert exc_info.value.retryable is True


def test_brevo_5xx_raises_rate_limited_as_retryable(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_response = _FakeResponse(503, text="down for maintenance")
    monkeypatch.setattr(email_module.requests, "post", Mock(return_value=fake_response))
    monkeypatch.setattr(email_module, "get_settings", lambda: _brevo_settings())

    with pytest.raises(ProviderRateLimitedError) as exc_info:
        email_module.send_otp_email(to_email="person@grameenphone.com", code="777888", ttl_minutes=10)
    assert exc_info.value.provider == "brevo"


def test_brevo_4xx_raises_permanent_with_provider_error_detail(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        email_module.requests, "post", Mock(return_value=_FakeResponse(400, text="invalid sender address"))
    )
    monkeypatch.setattr(email_module, "get_settings", lambda: _brevo_settings())

    with pytest.raises(ProviderPermanentError) as exc_info:
        email_module.send_otp_email(to_email="person@grameenphone.com", code="999000", ttl_minutes=10)
    assert exc_info.value.provider == "brevo"
    assert exc_info.value.retryable is False
    assert "invalid sender address" in str(exc_info.value)


def test_brevo_network_exception_raises_rate_limited_not_a_bare_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    import requests as requests_module

    def _raise_network_error(*args: Any, **kwargs: Any) -> Any:
        raise requests_module.ConnectionError("connection refused")

    monkeypatch.setattr(email_module.requests, "post", _raise_network_error)
    monkeypatch.setattr(email_module, "get_settings", lambda: _brevo_settings())

    with pytest.raises(ProviderRateLimitedError) as exc_info:
        email_module.send_otp_email(to_email="person@grameenphone.com", code="121212", ttl_minutes=10)
    assert exc_info.value.provider == "brevo"


def test_brevo_failure_never_logs_the_raw_otp_code(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger="app.services.email")
    monkeypatch.setattr(email_module.requests, "post", Mock(return_value=_FakeResponse(400, text="rejected")))
    monkeypatch.setattr(email_module, "get_settings", lambda: _brevo_settings())

    with pytest.raises(ProviderPermanentError):
        email_module.send_otp_email(to_email="person@grameenphone.com", code="345678", ttl_minutes=10)

    for record in caplog.records:
        haystack = record.getMessage() + " " + " ".join(str(v) for v in vars(record).values())
        assert "345678" not in haystack


# ── Resend: still fully working (non-default alternative) ─────────────────


def test_resend_still_uses_authorization_bearer_header(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_post = Mock(return_value=_FakeResponse(200, {"id": "resend-id"}))
    monkeypatch.setattr(email_module.requests, "post", fake_post)
    monkeypatch.setattr(email_module, "get_settings", lambda: _resend_settings())

    email_module.send_otp_email(to_email="person@grameenphone.com", code="246810", ttl_minutes=10)

    headers = fake_post.call_args.kwargs["headers"]
    assert headers["Authorization"] == "Bearer re_test_key"
    assert "api-key" not in headers


def test_resend_429_raises_rate_limited_with_provider_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(email_module.requests, "post", Mock(return_value=_FakeResponse(429)))
    monkeypatch.setattr(email_module, "get_settings", lambda: _resend_settings())

    with pytest.raises(ProviderRateLimitedError) as exc_info:
        email_module.send_otp_email(to_email="person@grameenphone.com", code="135791", ttl_minutes=10)
    assert exc_info.value.provider == "resend"


# ── Purpose changes copy only, never the security-relevant call shape ─────


def test_password_reset_purpose_changes_the_email_subject(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_post = Mock(return_value=_FakeResponse(201, {"messageId": "x@smtp-relay.mailin.fr"}))
    monkeypatch.setattr(email_module.requests, "post", fake_post)
    monkeypatch.setattr(email_module, "get_settings", lambda: _brevo_settings())

    email_module.send_otp_email(to_email="person@grameenphone.com", code="864209", ttl_minutes=10, purpose="signup")
    signup_subject = fake_post.call_args.kwargs["json"]["subject"]

    email_module.send_otp_email(
        to_email="person@grameenphone.com", code="864209", ttl_minutes=10, purpose="password_reset"
    )
    reset_subject = fake_post.call_args.kwargs["json"]["subject"]

    assert signup_subject != reset_subject
