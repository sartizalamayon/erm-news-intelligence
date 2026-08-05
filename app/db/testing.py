"""Test-only helper for standing up an isolated, migrated schema against
a real Postgres server. Lives under `app/db/` (ARCH-DB's territory, not
`tests/conftest.py`, which ARCH-TEST owns) so `tests/test_db_*.py` and
`tests/test_storage*.py` can share it without touching that file.

Why a real Postgres rather than a mock: this package's entire job is
Postgres-specific behavior — a partial unique index catching a
dedup-on-insert conflict, `pg_total_relation_size`, `ON CONFLICT`, a
generated `tsvector` column — none of which a mocked connection would
exercise honestly. GATE 0 rules out testcontainers/Docker for this
project, so the target is whatever real Postgres server is reachable at
`TEST_DATABASE_URL` (or `DATABASE_URL` as a fallback), which in CI today
is nothing (`.github/workflows/ci.yml` sets `DATABASE_URL=""` and has no
Postgres service) — see `pg_available()`, which every test in this
package checks first and skips cleanly rather than failing when no
server is reachable. Locally, point `TEST_DATABASE_URL` at any
throwaway Postgres 14+ (e.g. `postgresql:///erm_test` against a local
`postgres` install) and the whole suite runs for real.

Isolation: each test gets its own uniquely-named schema (not a new
database — faster, and needs no CREATEDB privilege) with `search_path`
pinned to it, migrated fresh via `app.db.migrate.run_migrations`, then
dropped on teardown. Tests never share state and can run with
`pytest -n auto` safely.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from contextlib import contextmanager

import psycopg

from app.db.connection import DictConnection
from app.db.migrate import run_migrations

_ENV_VARS = ("TEST_DATABASE_URL", "DATABASE_URL")
_DEFAULT_DSN = "postgresql:///erm_test"


def _candidate_dsn() -> str:
    for var in _ENV_VARS:
        value = os.environ.get(var)
        if value:
            return value
    return _DEFAULT_DSN


def pg_available() -> bool:
    try:
        with psycopg.connect(_candidate_dsn(), connect_timeout=2) as conn:
            conn.execute("SELECT 1")
        return True
    except psycopg.OperationalError:
        return False


@contextmanager
def migrated_schema() -> Iterator[DictConnection]:
    """Yield a psycopg connection pinned (via `search_path`) to a fresh,
    fully-migrated, uniquely-named schema. Caller is responsible for its
    own commit/rollback within the `with` block; the schema itself is
    always dropped on exit regardless of outcome.
    """
    schema = f"test_{uuid.uuid4().hex[:16]}"
    admin_conn = psycopg.connect(_candidate_dsn(), row_factory=psycopg.rows.dict_row, autocommit=True)
    try:
        admin_conn.execute(f'CREATE SCHEMA "{schema}"')
        conn = psycopg.connect(_candidate_dsn(), row_factory=psycopg.rows.dict_row, options=f"-c search_path={schema}")
        try:
            run_migrations(conn)
            yield conn
        finally:
            conn.close()
    finally:
        admin_conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        admin_conn.close()
