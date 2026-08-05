"""Deterministic, forward-only SQL migration runner (DoD #2).

GATE 0 disqualified heavyweight ORM/migration frameworks ("plain SQL
migration files with a small deterministic runner is the expected shape
unless you can justify otherwise"). This is that runner: it does exactly
one thing — apply every `migrations/*.sql` file that hasn't been applied
yet, in filename order, each inside its own transaction, and record what
it applied in a `schema_migrations` bookkeeping table. No down-migrations,
no branching, no ORM model layer — migration files are the schema's
source of truth, read top to bottom.

**Rule: never edit a migration file after it has shipped.** If a
previously-applied file's on-disk content no longer matches the checksum
recorded when it ran, `run_migrations` raises rather than silently
re-running or skipping it — that mismatch means someone edited history,
which is exactly the class of bug a deterministic runner exists to catch.
Fix forward with a new numbered file instead.

Usage
-----
CI (`.github/workflows/ci.yml`, once wired) and local dev:

    python -m app.db.migrate

Daily batch job (`app.batch.run_daily.main()`, once wired): call
`run_migrations()` first, before touching any other table. Migrations
are idempotent — on every normal day this is a single fast
`SELECT filename FROM schema_migrations` with nothing to apply — so
paying that cost once a day guarantees the batch job never runs against
a schema that hasn't caught up with the latest migration yet, without
needing a separate deploy step to run migrations out-of-band.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from app.db.connection import DictConnection, get_connection
from app.logging import get_logger

logger = get_logger(__name__)

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent.parent / "migrations"


def _migration_files(migrations_dir: Path) -> list[Path]:
    return sorted(p for p in migrations_dir.glob("*.sql") if p.is_file())


def _checksum(sql: str) -> str:
    return hashlib.sha256(sql.encode("utf-8")).hexdigest()


def ensure_migrations_table(conn: DictConnection) -> None:
    """Idempotent bootstrap of the tracking table itself — deliberately
    not migration 0000, so the very first `run_migrations()` call on a
    genuinely empty database has somewhere to record that 0001 ran.
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            filename    TEXT PRIMARY KEY,
            checksum    TEXT NOT NULL,
            applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    conn.commit()


def applied_migrations(conn: DictConnection) -> dict[str, str]:
    cur = conn.execute("SELECT filename, checksum FROM schema_migrations")
    return {row["filename"]: row["checksum"] for row in cur.fetchall()}


def run_migrations(
    conn: DictConnection | None = None,
    migrations_dir: Path = MIGRATIONS_DIR,
) -> list[str]:
    """Apply every not-yet-applied migration file, in filename order.

    Returns the filenames newly applied (empty list if already
    up to date — the common case on every batch run after the first).
    Opens its own connection via `app.db.connection.get_connection` if
    none is passed, so `python -m app.db.migrate` and CI can call this
    with zero setup.
    """
    if conn is not None:
        return _run_migrations(conn, migrations_dir)
    with get_connection() as owned_conn:
        return _run_migrations(owned_conn, migrations_dir)


def _run_migrations(conn: DictConnection, migrations_dir: Path) -> list[str]:
    ensure_migrations_table(conn)
    already = applied_migrations(conn)
    newly_applied: list[str] = []

    for path in _migration_files(migrations_dir):
        sql = path.read_text()
        checksum = _checksum(sql)

        if path.name in already:
            if already[path.name] != checksum:
                raise RuntimeError(
                    f"migration {path.name} has changed on disk since it was applied "
                    f"(checksum mismatch) — never edit a shipped migration; add a new "
                    f"numbered file instead"
                )
            continue

        with conn.transaction():
            conn.execute(sql)
            conn.execute(
                "INSERT INTO schema_migrations (filename, checksum) VALUES (%s, %s)",
                (path.name, checksum),
            )
        newly_applied.append(path.name)
        logger.info("migration_applied", extra={"filename": path.name})

    if not newly_applied:
        logger.info("migrations_up_to_date", extra={"count": len(already)})
    return newly_applied


def main() -> None:
    applied = run_migrations()
    if applied:
        print(f"Applied {len(applied)} migration(s): {', '.join(applied)}")
    else:
        print("Database already up to date.")


if __name__ == "__main__":
    main()
