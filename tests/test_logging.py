"""app.logging: redaction is the default path, not opt-in. If this test
ever fails, a secret-shaped field name is one `logger.info(..., extra=...)`
call away from leaking into GitHub Actions / Render logs.
"""

from __future__ import annotations

import json
import logging

from app.logging import REDACTED, StructuredFormatter, mask


def _format_record(**extra) -> dict:
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="a message",
        args=(),
        exc_info=None,
    )
    for key, value in extra.items():
        setattr(record, key, value)
    return json.loads(StructuredFormatter().format(record))


def test_sensitive_keys_are_redacted():
    payload = _format_record(api_key="sk-super-secret", otp_code="123456", password="hunter2")
    assert payload["api_key"] == REDACTED
    assert payload["otp_code"] == REDACTED
    assert payload["password"] == REDACTED


def test_non_sensitive_extra_passes_through():
    payload = _format_record(source="prothom_alo", count=5)
    assert payload["source"] == "prothom_alo"
    assert payload["count"] == 5


def test_mask_keeps_only_a_suffix():
    assert mask("sk-abcdef1234") == "*********1234"
    assert mask("ab") == "**"
