"""Typed access to the `settings` key/value table (0001_settings.sql).

Distinct from `app.config.Settings`: that module is process-boot config
(env vars, read once at startup — DATABASE_URL, SECRET_KEY, ...). This
module is runtime, admin-editable config that lives in the database —
the storage ceiling, retention windows, the OTP-allowed email domain —
values an admin should be able to change without a redeploy. DoD #4
requires exactly this split: "the ceiling as configuration not a
constant."
"""

from __future__ import annotations

from app.db.connection import DictConnection

# Mirrors the defaults seeded by 0001_settings.sql — used only as a
# last-resort fallback if a row is somehow missing (e.g. a hand-rolled
# test database), never as the primary source of truth.
_FALLBACK_DEFAULTS = {
    "storage_ceiling_mb": "500",
    "raw_retention_days": "90",
    "body_retention_days": "60",
    "allowed_email_domain": "grameenphone.com",
}


def get_setting(conn: DictConnection, key: str, default: str | None = None) -> str:
    row = conn.execute("SELECT value FROM settings WHERE key = %s", (key,)).fetchone()
    if row is not None:
        return str(row["value"])
    if default is not None:
        return default
    return _FALLBACK_DEFAULTS.get(key, "")


def get_setting_int(conn: DictConnection, key: str, default: int | None = None) -> int:
    raw = get_setting(conn, key, str(default) if default is not None else None)
    return int(raw)


def set_setting(conn: DictConnection, key: str, value: str) -> None:
    conn.execute(
        """
        INSERT INTO settings (key, value, updated_at) VALUES (%s, %s, now())
        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()
        """,
        (key, value),
    )
