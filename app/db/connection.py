"""Connection handling for the Postgres database (Supabase, ADR §1/§9.4).

No connection-pool library is added here. Supabase free tier allows 60
direct / 200 pooled connections (ADR §1); this app has exactly two
callers — the FastAPI process (human-scale request volume) and the
once-daily batch job (a single connection held for one ~3.5-minute run,
ADR §2's ≤207s target) — neither of which comes close to needing a pool
of our own. A short-lived `psycopg.connect()` per unit of work (one API
request, one batch run) is simpler and exactly as correct at this scale.
If request volume ever justifies real pooling, `psycopg_pool` is the
natural next dependency to flag — not something to pre-build speculatively
(GATE 0: lean, no ceremony beyond what's needed).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import psycopg
from psycopg.rows import dict_row

from app.config import get_settings
from app.errors import ConfigurationError

# Every connection this package hands out uses row_factory=dict_row (see
# get_connection below), so every function elsewhere in app/db and
# app/services that takes a connection parameter types it as this alias
# rather than bare `psycopg.Connection` (which mypy resolves to the
# tuple-row default and rejects `row["column"]`-style access).
DictConnection = psycopg.Connection[dict[str, Any]]


def get_dsn() -> str:
    """Read DATABASE_URL from app.config.Settings (ADR §1/§9.4's Supabase
    connection string). Raises ConfigurationError — never a bare
    exception, per app/errors.py's taxonomy — rather than letting
    psycopg fail on an empty DSN with a confusing driver-level error.
    """
    settings = get_settings()
    if not settings.database_url:
        raise ConfigurationError("DATABASE_URL is not configured")
    return settings.database_url


@contextmanager
def get_connection(dsn: str | None = None) -> Iterator[DictConnection]:
    """Open one connection scoped to a `with` block: commit on clean
    exit, rollback and re-raise on any exception, always close.

    `row_factory=dict_row` so query results come back as plain dicts
    (JSON-serializable, readable field access) — the lean alternative to
    an ORM's mapped objects for the amount of query surface this project
    actually has (GATE 0).
    """
    conn: DictConnection = psycopg.connect(dsn or get_dsn(), row_factory=dict_row)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
