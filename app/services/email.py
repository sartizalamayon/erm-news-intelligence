"""Resend email transport for OTP delivery.

ADR §9.3 (news-intel-rebuild/docs/architecture-decision-record.md) rules
out Supabase's built-in mailer -- verified at 2 messages/hour, explicitly
non-production, and it refuses delivery to any address outside the
Supabase project's own team, which would make it silently undeliverable
to every real @grameenphone.com recipient -- and approves Resend (100
emails/day, 3,000/month, free) as a new dependency in its place.

Per the AUTH-OTP mission brief, this calls Resend's REST API directly
over `requests` (already a dependency -- pyproject.toml) rather than
adding the `resend` SDK for what is, mechanically, one HTTP POST.

Configuration note -- why this reuses the `smtp_*` settings fields:
`app.config.Settings` provisioned generic `smtp_host`/`smtp_port`/
`smtp_username`/`smtp_password`/`smtp_from_email`/`smtp_use_tls` fields
"so AUTH-OTP can wire whichever vendor gets approved without a config
reshape" (that module's own docstring) -- written before Resend was
ruled in over classic SMTP. Resend's REST transport needs only an API
key and a verified From address, not a host/port/username. This package
does not own `app/config.py` (ARCH-REPO does -- see
migrations/0006_provider_configs.sql's header, which notes the same
constraint for a different field: "ARCH-DB does not own app/config.py").
Rather than leave email sending unconfigurable, this module reuses the
two fields that already have the right shape and the right sensitivity
annotation:
  * `settings.smtp_password` (repr=False, i.e. already treated as a
    secret in Settings' own type) holds the Resend API key.
  * `settings.smtp_from_email` holds the verified Resend From address.
`smtp_host` / `smtp_port` / `smtp_username` / `smtp_use_tls` are simply
unused by this transport. Flagged in the AUTH-OTP task report as a
follow-up: whoever owns app/config.py should rename these to
vendor-neutral names (`email_api_key` / `email_from_address`) once this
is the agreed shape, rather than this module inventing new Settings
fields it isn't allowed to add.
"""

from __future__ import annotations

import requests

from app.config import get_settings
from app.errors import ConfigurationError, ProviderPermanentError, ProviderRateLimitedError
from app.logging import get_logger, mask

logger = get_logger(__name__)

RESEND_API_URL = "https://api.resend.com/emails"
REQUEST_TIMEOUT_SECONDS = 10
PROVIDER_NAME = "resend"


def _subject_and_body(code: str, ttl_minutes: int) -> tuple[str, str]:
    subject = "Your ERM News Intelligence sign-in code"
    text = (
        f"Your verification code is {code}.\n\n"
        f"It expires in {ttl_minutes} minutes and can only be used once. "
        "If you did not request this, you can safely ignore this email."
    )
    return subject, text


def send_otp_email(*, to_email: str, code: str, ttl_minutes: int) -> None:
    """Send one OTP email via Resend's REST API.

    Always raises a subclass of `app.errors.ProviderError` on any
    non-2xx outcome (never a bare `requests` exception) -- `ProviderError`
    is the existing taxonomy entry whose own docstring already names
    "SMTP (OTP email)" / AUTH-OTP as its owner, and app.errors'
    registered exception handler already turns `retryable=True` into a
    503 and `retryable=False` into a 502 with zero changes needed to
    that file. `app.services.auth` catches this to log the failure and
    record it on the `otp_codes` row (DoD #5) before letting it
    propagate.

    The OTP code itself is never logged by this function under any
    circumstance -- only a masked form of the recipient address on the
    failure paths below (`app.logging`'s key-based redaction does not
    cover "email", so this masks explicitly rather than relying on it).
    """
    settings = get_settings()
    api_key = settings.smtp_password
    from_email = settings.smtp_from_email
    if not api_key or not from_email:
        raise ConfigurationError(
            "Resend is not configured -- SMTP_PASSWORD (holding the Resend API "
            "key) and SMTP_FROM_EMAIL (the verified From address) must both be set."
        )

    subject, text = _subject_and_body(code, ttl_minutes)
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
            "Resend request failed (network/timeout)", provider=PROVIDER_NAME, cause=exc
        ) from exc

    if response.status_code == 429:
        # Resend's own rate-limit/quota-exceeded response. Retryable in
        # principle (tomorrow's quota resets), but request_otp's own
        # pre-send daily-count check (app/services/auth.py) is meant to
        # catch this *before* we ever get here in the common case --
        # this branch is the backstop for when our local count and
        # Resend's own bookkeeping disagree.
        logger.error("resend_quota_exceeded", extra={"email_masked": mask(to_email), "status_code": 429})
        raise ProviderRateLimitedError("Resend rate limit or daily quota exceeded", provider=PROVIDER_NAME)

    if response.status_code >= 500:
        logger.error(
            "resend_server_error", extra={"email_masked": mask(to_email), "status_code": response.status_code}
        )
        raise ProviderRateLimitedError(f"Resend server error (HTTP {response.status_code})", provider=PROVIDER_NAME)

    if response.status_code >= 400:
        logger.error(
            "resend_rejected_request", extra={"email_masked": mask(to_email), "status_code": response.status_code}
        )
        raise ProviderPermanentError(
            f"Resend rejected the request (HTTP {response.status_code}): {response.text[:200]}",
            provider=PROVIDER_NAME,
        )
