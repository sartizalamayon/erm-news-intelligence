"""Production startup must refuse to boot on the two configuration
mistakes that Render reports as a completely healthy deploy.

Both defects covered here share one shape, and it is the worst shape a
deploy bug can have: the process starts, `/health/ready` returns 200,
Render marks the service live, autoDeploy goes green -- and the system is
already broken in a way nothing will report until a human hits it.

  * A blank Brevo key (or from-address) while `EMAIL_PROVIDER=brevo`.
    `app/services/email.py` treats "no key OR no from-address" as
    unconfigured, and `_console_fallback_allowed` hard-disables the
    console fallback in production/staging, so `send_otp_email` raises
    `ProviderPermanentError` for every request. The only admin account
    (`migrations/0009_seed_admin.sql`) is seeded with no `password_hash`,
    so an emailed OTP is the sole route to obtaining one. Nobody -- not
    even the owner -- can ever log in, and the only symptom is a generic
    "Email delivery is not configured. Please contact an administrator."
    from an API that is otherwise perfectly healthy.

  * A `SECRET_KEY` that is not a valid Fernet key. `app/db/crypto.py`
    calls `Fernet(key.encode())`, which needs 32 url-safe base64-encoded
    bytes; anything else raises `ValueError`. Nothing calls it at boot,
    so the process serves traffic normally until the first admin saves an
    LLM provider API key -- at which point it 500s, days and screens away
    from the env var that actually caused it.

Every test here therefore asserts on the MESSAGE as well as the raise:
the whole value of failing fast is telling the operator -- who is looking
at a Render dashboard full of blank `sync: false` secret fields -- which
one to fill in.
"""

from __future__ import annotations

from typing import get_args

import pytest
from cryptography.fernet import Fernet

from app.config import (
    _EMAIL_PROVIDER_REQUIRED_ENV,
    EmailProvider,
    Settings,
    validate_production_settings,
)
from app.errors import ConfigurationError


def _production_settings(**overrides: object) -> Settings:
    """A production Settings object that is valid apart from whatever the
    individual test deliberately breaks -- so a raise can only ever be
    attributable to that one override.
    """
    base: dict[str, object] = {
        "app_env": "production",
        "database_url": "postgresql://user:pw@host/db",
        "secret_key": Fernet.generate_key().decode(),
        "groq_api_key": "a-real-key",
        "email_provider": "brevo",
        "brevo_api_key": "a-real-key",
        "brevo_from_email": "otp@example.test",
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


def test_the_fully_configured_baseline_starts():
    """Guards the guard: every test below breaks exactly one thing, which
    only means something if the unbroken version passes.
    """
    validate_production_settings(_production_settings())


# ── B1: the selected email provider must be able to send ─────────────


def test_blank_brevo_api_key_fails_production_startup_by_name():
    with pytest.raises(ConfigurationError) as exc:
        validate_production_settings(_production_settings(brevo_api_key=""))
    message = str(exc.value)
    assert "BREVO_API_KEY" in message
    # The reason it is required matters as much as the name: the same var
    # is genuinely inert when EMAIL_PROVIDER=resend, and an operator who
    # cannot see why it is being demanded may "fix" it by switching
    # providers rather than by supplying the key.
    assert "EMAIL_PROVIDER=brevo" in message


def test_blank_brevo_from_email_fails_production_startup_by_name():
    """A key with no verified sender address is exactly as unable to send
    as no key at all -- app.services.email requires BOTH before it
    considers the transport configured (`configured = bool(api_key and
    from_email)`), so both have to be validated.
    """
    with pytest.raises(ConfigurationError) as exc:
        validate_production_settings(_production_settings(brevo_from_email=""))
    assert "BREVO_FROM_EMAIL" in str(exc.value)


def test_resend_selected_validates_resend_vars_not_brevo_ones():
    """Switching the provider switches which secrets are mandatory.

    render.yaml keeps both blocks declared on purpose (Resend stays a
    working alternative if a verified sending domain ever exists), so the
    inactive provider's two secrets are routinely blank in a perfectly
    valid deployment. Failing a deploy over a secret its own
    EMAIL_PROVIDER never reads would be a false alarm that teaches the
    operator to fill in values that do nothing.
    """
    settings = _production_settings(
        email_provider="resend",
        resend_api_key="",
        resend_from_email="",
        brevo_api_key="",
        brevo_from_email="",
    )
    with pytest.raises(ConfigurationError) as exc:
        validate_production_settings(settings)
    message = str(exc.value)
    assert "RESEND_API_KEY" in message
    assert "RESEND_FROM_EMAIL" in message
    assert "BREVO" not in message


def test_resend_selected_and_configured_starts_with_brevo_blank():
    validate_production_settings(
        _production_settings(
            email_provider="resend",
            resend_api_key="a-real-key",
            resend_from_email="otp@example.test",
            brevo_api_key="",
            brevo_from_email="",
        )
    )


def test_every_email_provider_literal_member_has_required_env_declared():
    """The mechanism, not just the two current instances.

    `EmailProvider` is the single source of truth for which transports
    exist; `_EMAIL_PROVIDER_REQUIRED_ENV` is the single source of truth
    for what each one cannot start without. If a third provider is ever
    added to the first and not the second, production would silently skip
    validating it -- which is precisely how Brevo (added later, as an
    additive owner-ruled switch) came to be the default transport with no
    startup check at all. app/config.py raises at IMPORT time if this
    drifts; this test states the invariant in the place someone reads
    when they are adding a provider.
    """
    assert set(get_args(EmailProvider)) == set(_EMAIL_PROVIDER_REQUIRED_ENV)
    for provider, required in _EMAIL_PROVIDER_REQUIRED_ENV.items():
        assert required, f"{provider} must declare at least one required env var"
        for env_name, field_name in required:
            assert env_name.isupper(), f"{env_name} should be the ENV VAR name the operator sets"
            assert field_name in Settings.model_fields, f"{field_name} is not a Settings field"


def test_non_production_environments_are_not_gated_on_email_config():
    """Local dev and CI run with every provider secret blank on purpose
    (tests/conftest.py blanks them); `app.services.email`'s console
    fallback covers login there. This check is production-only, like
    every other check in validate_production_settings.
    """
    validate_production_settings(Settings(app_env="development", email_provider="brevo", brevo_api_key=""))
    validate_production_settings(Settings(app_env="test", email_provider="brevo", brevo_api_key=""))


# ── S1: SECRET_KEY must be a key Fernet will actually accept ─────────


@pytest.mark.parametrize(
    "bad_key",
    [
        pytest.param("a-real-key", id="looks-like-a-secret-but-is-not-base64"),
        pytest.param("correct horse battery staple", id="passphrase"),
        pytest.param("0123456789abcdef0123456789abcdef", id="32-raw-chars-not-base64-encoded"),
        pytest.param(Fernet.generate_key().decode()[:-4], id="truncated-paste"),
    ],
)
def test_invalid_fernet_secret_key_fails_production_startup(bad_key: str):
    """Each of these is truthy, so the old truthiness-only check passed
    every one of them. `Fernet` accepts none of them.
    """
    with pytest.raises(ConfigurationError) as exc:
        validate_production_settings(_production_settings(secret_key=bad_key))
    message = str(exc.value)
    assert "SECRET_KEY" in message
    # The message has to be actionable: an operator who reads "invalid"
    # and nothing else will paste in another arbitrary string.
    assert "Fernet.generate_key()" in message


def test_valid_fernet_secret_key_is_accepted_and_actually_usable():
    """Not just "does not raise" -- the accepted value is round-tripped
    through the same Fernet API app/db/crypto.py uses, so this test would
    still be meaningful if the validation were ever rewritten to check
    the shape by hand rather than by construction.
    """
    key = Fernet.generate_key().decode()
    validate_production_settings(_production_settings(secret_key=key))
    assert Fernet(key.encode()).decrypt(Fernet(key.encode()).encrypt(b"provider-api-key")) == b"provider-api-key"


def test_missing_secret_key_is_still_reported_as_missing_not_as_malformed():
    """An empty SECRET_KEY has always been reported by the "missing
    required production configuration" list, and should keep being --
    "you did not set it" and "what you set is not a Fernet key" are
    different operator problems with different fixes.
    """
    with pytest.raises(ConfigurationError) as exc:
        validate_production_settings(_production_settings(secret_key=""))
    message = str(exc.value)
    assert "Missing required production configuration" in message
    assert "SECRET_KEY" in message
    assert "Fernet" not in message
