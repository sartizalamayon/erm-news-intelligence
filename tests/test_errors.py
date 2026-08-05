"""app.errors: the exception taxonomy carries the fields callers branch
on (retryable, source, provider) and every concrete class is an AppError
subclass — the property app.api.error_handlers relies on to catch
"everything modeled" without a bare `except Exception`.
"""

from __future__ import annotations

import pytest

from app.errors import (
    AppError,
    ConfigurationError,
    ProviderError,
    ProviderPermanentError,
    ProviderRateLimitedError,
    SourceConfigError,
    SourceError,
    SourceFetchError,
    ValidationError,
)

ALL_CONCRETE = [
    ConfigurationError,
    ValidationError,
    SourceFetchError,
    SourceConfigError,
    ProviderRateLimitedError,
    ProviderPermanentError,
]


@pytest.mark.parametrize("exc_cls", ALL_CONCRETE)
def test_every_taxonomy_member_is_an_apperror(exc_cls):
    assert issubclass(exc_cls, AppError)


def test_source_error_carries_source_name():
    exc = SourceFetchError("timed out", source="prothom_alo")
    assert isinstance(exc, SourceError)
    assert exc.source == "prothom_alo"


def test_rate_limited_is_retryable_permanent_is_not():
    rate_limited = ProviderRateLimitedError("quota hit", provider="groq", retry_after=30.0)
    permanent = ProviderPermanentError("bad api key", provider="groq")

    assert isinstance(rate_limited, ProviderError)
    assert rate_limited.retryable is True
    assert rate_limited.retry_after == 30.0

    assert isinstance(permanent, ProviderError)
    assert permanent.retryable is False
