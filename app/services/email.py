"""Email transport for OTP delivery -- Resend or Brevo, selected by
`app.config.Settings.email_provider`.

OWNER-RULED PROVIDER CHANGE: ADR §9.3 originally approved Resend (100
emails/day, 3,000/month, free) as the OTP transport. Under OTP-only auth
that was fine; under the new one-email-per-account-lifetime model
(app/services/auth.py's module docstring) the constraint that actually
matters flipped -- Resend's sandbox `onboarding@resend.dev` sender can
only deliver to the Resend account owner's own address; sending to real
@grameenphone.com recipients needs a verified sending domain, and
grameenphone.com can only be verified by Grameenphone IT, which the
owner has ruled out. Brevo needs only an API key and an unverified
"From" address -- LIVE-VERIFIED working: two real emails delivered to a
@grameenphone.com inbox (landed in Junk, expected -- see below). Brevo
is therefore the default (`email_provider` in app.config.Settings);
Resend is kept fully working as the non-default alternative (e.g. once
a verified domain exists), selected by the same switch.

Both transports call their provider's plain REST API directly via
`requests` (already a dependency) rather than adding an SDK, for one
HTTP POST each:
  * Resend:  POST https://api.resend.com/emails
             Authorization: Bearer <key>
  * Brevo:   POST https://api.brevo.com/v3/smtp/email
             api-key: <key>  (NOT "Authorization: Bearer" -- Brevo's own
             convention, confirmed against a real successful call)

Deliverability (accepted risk, not solved here): Brevo's own dashboard
flags a freemail "From" address (gmail.com, in this deployment) as
non-compliant with Google/Yahoo/Microsoft's 2024+ bulk-sender rules.
Mail Brevo ACCEPTS (2xx from the API) may still land in spam/junk or be
silently dropped by the recipient's filter -- exactly what was observed
in live verification. This module cannot fix that (it is a receiving-
mailbox-provider policy decision, not a bug), so it focuses on making
delivery OBSERVABLE instead: the provider's own `messageId` is logged
on a successful Brevo send, and any failure (network, 4xx, 5xx, quota)
is persisted on the `otp_codes` row with the provider's own error text
by app.services.auth's request_otp() -- the existing `send_status` /
`send_error` audit trail (and the admin-only GET /auth/otp-failures
view) works identically regardless of which provider is active.

The OTP code itself is never logged by this function under any
circumstance -- only a masked form of the recipient address on the
failure/success paths below (`app.logging`'s key-based redaction does
not cover "email", so this masks explicitly rather than relying on it).
"""

from __future__ import annotations

from typing import Any

import requests

from app.config import Settings, get_settings
from app.errors import ProviderPermanentError, ProviderRateLimitedError
from app.logging import get_logger, mask

logger = get_logger(__name__)

RESEND_API_URL = "https://api.resend.com/emails"
BREVO_API_URL = "https://api.brevo.com/v3/smtp/email"
REQUEST_TIMEOUT_SECONDS = 10


def _console_fallback_allowed(settings: Settings) -> bool:
    """HARD GATE: production/staging can NEVER use the console fallback,
    regardless of otp_console_fallback_enabled -- an unconfigured
    provider must fail loudly there, not quietly print a login code.
    Outside those two environments, the (default-True) flag decides.
    """
    if settings.app_env in ("production", "staging"):
        return False
    return settings.otp_console_fallback_enabled


def _subject_and_body(code: str, ttl_minutes: int, purpose: str) -> tuple[str, str]:
    """`purpose` ("signup" | "password_reset", app.services.auth's
    _OTP_PURPOSES) only ever changes the email copy -- never any
    security property of the OTP itself.
    """
    if purpose == "password_reset":
        subject = "Reset your ERM News Intelligence password"
        text = (
            f"Your password reset code is {code}.\n\n"
            f"It expires in {ttl_minutes} minutes and can only be used once. "
            "Enter it along with your new password to finish resetting it. "
            "If you did not request this, you can safely ignore this email."
        )
        return subject, text
    subject = "Verify your email for ERM News Intelligence"
    text = (
        f"Your verification code is {code}.\n\n"
        f"It expires in {ttl_minutes} minutes and can only be used once. "
        "Enter it along with a new password to finish setting up your account. "
        "If you did not request this, you can safely ignore this email."
    )
    return subject, text


def send_otp_email(*, to_email: str, code: str, ttl_minutes: int, purpose: str = "signup") -> None:
    """Send one OTP email via the configured provider (Brevo by default,
    Resend as the alternative -- app.config.Settings.email_provider).

    Always raises a subclass of `app.errors.ProviderError` on any
    non-2xx outcome (never a bare `requests` exception) -- `ProviderError`
    is the existing taxonomy entry whose own docstring already names
    "SMTP (OTP email)" / AUTH-OTP as its owner, and app.errors'
    registered exception handler already turns `retryable=True` into a
    503 and `retryable=False` into a 502 with zero changes needed to
    that file. `app.services.auth` catches this to log the failure and
    record it on the `otp_codes` row (DoD #5) before letting it
    propagate.
    """
    settings = get_settings()
    provider = settings.email_provider

    if provider == "brevo":
        api_key = settings.brevo_api_key
        from_email = settings.brevo_from_email
        configured = bool(api_key and from_email)
    else:
        api_key = settings.resend_api_key
        from_email = settings.resend_from_email
        configured = bool(api_key and from_email)

    if not configured:
        if _console_fallback_allowed(settings):
            # DEV-ONLY: the code is deliberately in the MESSAGE text, not
            # an `extra=` field -- app.logging redacts extra keys matching
            # "otp" (SENSITIVE_KEY_SUBSTRINGS), which would hide it here
            # too. This is the one intentional exception to "never log an
            # OTP code," gated by _console_fallback_allowed() to be
            # impossible outside development/test.
            logger.warning(
                f"DEV_OTP_FALLBACK: no email provider configured (app_env={settings.app_env}, "
                f"provider={provider}) -- code={code} for {mask(to_email)} (expires in {ttl_minutes}m)"
            )
            return
        # Not falling back (production/staging, or the flag is off): this
        # is an operator problem, not the caller's fault. The message
        # below is what reaches the client via ProviderError's existing
        # handler (app/api/error_handlers.py, unmodified) -- deliberately
        # generic, no env var names, no paths. Full detail stays here,
        # server-side only.
        logger.error("email_provider_not_configured", extra={"app_env": settings.app_env, "provider": provider})
        raise ProviderPermanentError(
            "Email delivery is not configured. Please contact an administrator.",
            provider=provider,
        )

    subject, text = _subject_and_body(code, ttl_minutes, purpose)

    if provider == "brevo":
        _send_via_brevo(
            to_email=to_email,
            subject=subject,
            text=text,
            api_key=api_key,
            from_email=from_email,
            from_name=settings.brevo_from_name,
        )
    else:
        _send_via_resend(to_email=to_email, subject=subject, text=text, api_key=api_key, from_email=from_email)


def _send_via_resend(*, to_email: str, subject: str, text: str, api_key: str, from_email: str) -> None:
    payload = {"from": from_email, "to": [to_email], "subject": subject, "text": text}
    try:
        response = requests.post(
            RESEND_API_URL,
            json=payload,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        logger.error("resend_request_exception", extra={"email_masked": mask(to_email)})
        raise ProviderRateLimitedError(
            "Resend request failed (network/timeout)", provider="resend", cause=exc
        ) from exc

    _raise_for_provider_status(response, provider="resend", to_email=to_email)
    logger.info("resend_send_succeeded", extra={"email_masked": mask(to_email)})


def _send_via_brevo(
    *, to_email: str, subject: str, text: str, api_key: str, from_email: str, from_name: str
) -> None:
    """Body shape and headers below are exactly what was live-verified
    (two real deliveries to a @grameenphone.com inbox) -- see this
    module's docstring. `api-key`, not `Authorization: Bearer`, is
    Brevo's own convention.
    """
    payload: dict[str, Any] = {
        "sender": {"name": from_name or "ERM", "email": from_email},
        "to": [{"email": to_email}],
        "subject": subject,
        "textContent": text,
    }
    try:
        response = requests.post(
            BREVO_API_URL,
            json=payload,
            headers={"api-key": api_key, "Content-Type": "application/json", "accept": "application/json"},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    except requests.RequestException as exc:
        logger.error("brevo_request_exception", extra={"email_masked": mask(to_email)})
        raise ProviderRateLimitedError(
            "Brevo request failed (network/timeout)", provider="brevo", cause=exc
        ) from exc

    _raise_for_provider_status(response, provider="brevo", to_email=to_email)

    # Observability (this module's docstring): log the provider's own
    # messageId on success. Never fails the send if the body doesn't
    # parse as expected -- delivery already succeeded (2xx) by this
    # point; a malformed/missing messageId is a logging nicety, not a
    # reason to raise.
    message_id = ""
    try:
        message_id = str(response.json().get("messageId", ""))
    except ValueError:
        pass
    logger.info("brevo_send_succeeded", extra={"email_masked": mask(to_email), "message_id": message_id})


def _raise_for_provider_status(response: requests.Response, *, provider: str, to_email: str) -> None:
    """Shared 429 / 5xx / other-4xx handling for both transports --
    identical status-code semantics (retryable rate/quota vs. retryable
    server error vs. permanent rejection), just parameterized by
    provider name so the resulting ProviderError carries the right one
    for app.services.auth's send_status/send_error bookkeeping (DoD #5)
    and the client-facing 502/503 mapping (app.errors' existing handler).
    """
    if response.status_code == 429:
        logger.error(f"{provider}_quota_exceeded", extra={"email_masked": mask(to_email), "status_code": 429})
        raise ProviderRateLimitedError(f"{provider} rate limit or daily quota exceeded", provider=provider)

    if response.status_code >= 500:
        logger.error(
            f"{provider}_server_error", extra={"email_masked": mask(to_email), "status_code": response.status_code}
        )
        raise ProviderRateLimitedError(f"{provider} server error (HTTP {response.status_code})", provider=provider)

    if response.status_code >= 400:
        logger.error(
            f"{provider}_rejected_request",
            extra={"email_masked": mask(to_email), "status_code": response.status_code},
        )
        raise ProviderPermanentError(
            f"{provider} rejected the request (HTTP {response.status_code}): {response.text[:200]}",
            provider=provider,
        )
