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
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import psycopg

from app.db.connection import DictConnection, get_connection
from app.logging import get_logger

logger = get_logger(__name__)

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent.parent / "migrations"

# Postgres advisory-lock key for "somebody is running migrations against
# this database". Any stable bigint works — advisory locks are a pure
# application convention, Postgres attaches no meaning to the number —
# so this is `sha256(b"erm.schema_migrations")`'s first 8 bytes read as a
# signed big-endian integer, which makes it reproducible from its own
# name and vanishingly unlikely to collide with an unrelated advisory
# lock some future code (or Supabase itself) takes on the same database.
MIGRATION_ADVISORY_LOCK_KEY = -9139213102331722929


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


@contextmanager
def _migration_lock(conn: DictConnection) -> Iterator[None]:
    """Serialize the whole migration run behind one Postgres SESSION-level
    advisory lock, so a second runner WAITS instead of racing.

    This is not defensive programming against a hypothetical. Two things
    run `python -m app.db.migrate` against the same database on their own
    schedules and neither knows about the other:

      * render.yaml's `startCommand` chains it into uvicorn with `&&`.
        On a Render FREE web service that fires on every deploy AND on
        every wake from the 15-minute idle sleep — i.e. at times decided
        by whoever happens to open the site, not by us.
      * .github/workflows/daily-batch.yml runs the same command at 00:00
        UTC, and `app.batch.run_daily` reaches the same code path again.

    `CREATE TABLE IF NOT EXISTS` is NOT race-safe in Postgres: the
    existence check and the create are not atomic, so two sessions can
    both see "not there" and both try to create it. Reproduced here
    against a single fresh database with two parallel runners, and it
    reproduced 5 times out of 5:

        UniqueViolation: duplicate key value violates unique constraint
        "pg_type_typname_nsp_index"
        DETAIL: Key (typname, typnamespace)=(schema_migrations, 2200)
        already exists.

    What that costs in production is out of all proportion to how obscure
    it looks: `&&` short-circuits, so uvicorn never starts, and the
    cold-start wake or deploy that lost the race dies with a raw psycopg
    traceback in the Render log. The service is simply down until
    something retries it.

    SESSION-level (`pg_advisory_lock`), deliberately, not transaction-
    level (`pg_advisory_xact_lock`): the run spans several separate
    transactions — `ensure_migrations_table` commits, then each migration
    file gets its own `conn.transaction()` block — so a lock scoped to
    any one of them would be released while the rest of the run is still
    going, which is the same race with extra steps. The flip side of
    session scope is that Postgres will not release it for us on commit
    or rollback, which is why the `finally` below is not optional.

    No `lock_timeout` is set on the wait, on purpose. The realistic
    contention here is one runner waiting a few seconds for another to
    finish an idempotent no-op; a timeout would convert that harmless
    wait back into exactly the loud deploy failure this exists to remove,
    and both callers already sit under their own outer ceilings (Render's
    deploy timeout, the daily job's `timeout-minutes: 150`) if a holder
    ever genuinely wedges.
    """
    conn.execute("SELECT pg_advisory_lock(%s)", (MIGRATION_ADVISORY_LOCK_KEY,))
    try:
        yield
    finally:
        try:
            conn.execute("SELECT pg_advisory_unlock(%s)", (MIGRATION_ADVISORY_LOCK_KEY,))
        except psycopg.Error:
            # A migration raised mid-transaction and left this connection
            # in the aborted-transaction state, where Postgres rejects
            # every command except ROLLBACK — including the unlock. Roll
            # back (there is nothing left to lose: the failed migration's
            # own transaction was already rolled back by the
            # `conn.transaction()` block that raised) and release for
            # real, so the NEXT runner isn't left waiting on a lock held
            # by a connection that is about to be handed back or reused.
            # Closing the connection would release it too, but only
            # `run_migrations()`'s own short-lived connection is
            # guaranteed to be closed straight after — a caller-supplied
            # connection (tests, app.batch.run_daily) can live much
            # longer than this call does.
            conn.rollback()
            conn.execute("SELECT pg_advisory_unlock(%s)", (MIGRATION_ADVISORY_LOCK_KEY,))


def _run_migrations(conn: DictConnection, migrations_dir: Path) -> list[str]:
    with _migration_lock(conn):
        applied = _apply_migrations(conn, migrations_dir)
        # COMMIT BEFORE THE LOCK IS RELEASED — this line is the other half
        # of the fix, and leaving it out makes the lock decorative.
        #
        # Every migration file is applied inside `conn.transaction()`, and
        # because a transaction is already open by then (the SELECT in
        # `applied_migrations`, and now the lock acquisition itself),
        # psycopg implements those as SAVEPOINTs inside one outer
        # transaction rather than as independent commits. Nothing this
        # function does is therefore durable until somebody commits — and
        # the somebody used to be the CALLER, after this returned.
        #
        # Measured, not theorised: with the advisory lock in place but
        # this commit absent, two parallel runners against one fresh
        # database still failed 6 times out of 6 (`DuplicateTable:
        # relation "settings" already exists`). The second runner waited
        # for the lock exactly as designed, got it the moment the first
        # runner's `finally` released it, and then read an EMPTY
        # `schema_migrations` — because the first runner's inserts were
        # still sitting in an uncommitted transaction — so it dutifully
        # applied all 26 files a second time. A lock released before the
        # work it protects is visible to the next waiter protects nothing.
        #
        # Committing here is also the honest semantics for a migration
        # runner: "0007 has been applied" is a fact about the database,
        # not a pending edit for a caller to accept or discard.
        # `ensure_migrations_table` already commits on this same
        # connection, so this does not introduce caller-visible committing
        # behaviour that wasn't already there.
        conn.commit()
        return applied


def _apply_migrations(conn: DictConnection, migrations_dir: Path) -> list[str]:
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
