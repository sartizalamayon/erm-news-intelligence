"""app.config: defaults are safe, and the production fail-fast check
actually fires. Covers DoD #4's "configuration errors fail fast at
startup" without needing any real secret or network access.
"""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet

from app.config import Settings, validate_production_settings
from app.errors import ConfigurationError


def test_defaults_are_safe_for_dev_and_test():
    settings = Settings(app_env="test")
    assert settings.database_url == ""
    assert settings.groq_api_key == ""
    # Should not raise: non-production environments skip the fail-fast check.
    validate_production_settings(settings)


def test_production_without_secrets_fails_fast():
    settings = Settings(
        app_env="production",
        database_url="",
        secret_key="",
        groq_api_key="",
        gemini_api_key="",
    )
    with pytest.raises(ConfigurationError):
        validate_production_settings(settings)


def test_production_with_required_values_passes():
    """`secret_key` is a REAL generated Fernet key and the Brevo pair is
    filled in, because both are now startup-validated: SECRET_KEY has to
    be something `cryptography.fernet.Fernet` accepts (it was previously
    only checked for truthiness, so "a-real-key" passed here and then
    blew up the first time an admin saved a provider key), and whichever
    transport `email_provider` selects -- "brevo" by default -- has to
    have its API key and from-address. See
    tests/test_deploy_config_fail_fast.py for the dedicated coverage of
    both failure modes.
    """
    settings = Settings(
        app_env="production",
        database_url="postgresql://user:pw@host/db",
        secret_key=Fernet.generate_key().decode(),
        groq_api_key="a-real-key",
        brevo_api_key="a-real-key",
        brevo_from_email="otp@example.test",
    )
    validate_production_settings(settings)  # should not raise


def test_cors_origins_parsed_from_comma_separated_string():
    settings = Settings(cors_allow_origins="https://a.example, https://b.example")
    assert settings.cors_allow_origins_list == ["https://a.example", "https://b.example"]
