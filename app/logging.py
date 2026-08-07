"""Structured, level-configurable logging — safe for both the daily batch
job (progress must be readable in GitHub Actions logs) and the web
process (Render).

Every line is a single JSON object on stdout: timestamp, level, logger
name, message, plus any `extra=` fields the caller attaches. JSON was
chosen over a plain-text format because it's greppable/parseable in both
consumers without adding a logging framework dependency — this is stdlib
`logging` with one custom Formatter, nothing else.

Redaction is the default path, not opt-in: `StructuredFormatter` redacts
any extra field whose *key* looks sensitive before it is ever serialized,
so a call like `logger.info("token refreshed", extra={"api_key": key})`
cannot leak the value by accident. `mask()` is provided for the one case
redaction-by-key can't cover: deliberately logging a *masked* value (e.g.
the "masked suffix" ADR §9.1 requires when displaying a stored provider
key back to an admin).

This module never logs secrets, API keys, or OTP codes by name-based
heuristic; if a new sensitive field name is introduced elsewhere in the
app, add it to SENSITIVE_KEY_SUBSTRINGS below rather than relying on
callers to remember not to log it.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from datetime import datetime, timezone
from typing import Any

REDACTED = "***REDACTED***"

# Matched case-insensitively against each extra field's *key* (substring,
# not exact match) so e.g. "groq_api_key", "smtp_password", "otp_code",
# "authorization" are all caught without enumerating every field name.
SENSITIVE_KEY_SUBSTRINGS = (
    "password",
    "api_key",
    "apikey",
    "token",
    "secret",
    "otp",
    "authorization",
    # HARDEN mission secret-hygiene sweep (docs/security.md): nothing in
    # the codebase logs these today (verified by grepping every
    # `extra={...}` call site), but app.config.Settings.database_url is
    # a full connection string with an embedded password
    # (postgresql://user:PASSWORD@host/db) and none of the substrings
    # above would have caught a field literally named "database_url" or
    # "dsn" if a future call ever did `extra={"database_url": ...}`.
    # Covering it here is cheap, additive, and closes that gap before it
    # can ever be hit rather than after.
    "database_url",
    "dsn",
)
_SENSITIVE_PATTERN = re.compile("|".join(re.escape(s) for s in SENSITIVE_KEY_SUBSTRINGS), re.IGNORECASE)

# Attributes every stdlib LogRecord carries — anything else on the record
# is a caller-supplied `extra` field and is a redaction candidate.
_STANDARD_RECORD_ATTRS = frozenset(vars(logging.LogRecord("", 0, "", 0, "", None, None)).keys())


def _redact_extra(key: str, value: Any) -> Any:
    if _SENSITIVE_PATTERN.search(key):
        return REDACTED
    return value


def mask(value: str, keep: int = 4) -> str:
    """Return `value` with all but the last `keep` characters replaced,
    e.g. mask("sk-abcdef1234") -> "****1234". For deliberately displaying
    a masked suffix (ADR §9.1) — never for logging a value wholesale.
    """
    if not value:
        return ""
    if len(value) <= keep:
        return "*" * len(value)
    return "*" * (len(value) - keep) + value[-keep:]


class StructuredFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        extras = {
            key: _redact_extra(key, value)
            for key, value in vars(record).items()
            if key not in _STANDARD_RECORD_ATTRS
        }
        payload.update(extras)
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: str | None = None) -> None:
    """Idempotent: safe to call multiple times (app startup, batch
    startup, tests) — always resets to exactly one stdout handler rather
    than accumulating duplicates.
    """
    resolved_level = (level or os.environ.get("LOG_LEVEL") or "INFO").upper()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(StructuredFormatter())

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(resolved_level)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
