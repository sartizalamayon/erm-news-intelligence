"""Two migration runners against one database must not race.

The deploy shape makes this inevitable rather than unlikely. `render.yaml`
chains `python -m app.db.migrate && uvicorn ...` into the API's
`startCommand`, and on a Render FREE web service that runs on every
deploy AND on every wake from the 15-minute idle sleep -- i.e. at moments
chosen by whoever happens to open the site. `.github/workflows/
daily-batch.yml` runs the same command at 00:00 UTC, and
`app.batch.run_daily` reaches the same code path again. Nothing
coordinates them.

`CREATE TABLE IF NOT EXISTS` is not race-safe in Postgres (the existence
check and the create are not atomic), so before `app.db.migrate` took a
lock, two simultaneous runs against one fresh database failed like this
-- reproduced 5 times out of 5 with two parallel OS processes:

    UniqueViolation: duplicate key value violates unique constraint
    "pg_type_typname_nsp_index"
    DETAIL: Key (typname, typnamespace)=(schema_migrations, 2200)
    already exists.

`&&` short-circuits, so the loser's uvicorn never starts: the deploy or
the cold-start wake dies with a raw psycopg traceback and the service is
down until something retries.

These tests pin BOTH halves of the fix, because the first half alone was
measurably useless: the lock has to be held, and the work it protects has
to be COMMITTED before it is released. With the lock in place but the
commit missing, two parallel runners still failed 6 times out of 6 --
the waiter got the lock the instant the holder released it, read an
empty `schema_migrations` (the holder's inserts were still uncommitted),
and applied all 26 files a second time.
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager

import psycopg
import pytest

from app.db.migrate import MIGRATION_ADVISORY_LOCK_KEY, MIGRATIONS_DIR, run_migrations

# `_candidate_dsn` is the same TEST_DATABASE_URL -> DATABASE_URL ->
# local-default resolution every other database test resolves against
# (via `migrated_schema`). Imported rather than re-implemented so this
# file can never end up pointed at a different server than the rest of
# the suite.
from app.db.testing import _candidate_dsn, pg_available

pytestmark = pytest.mark.skipif(not pg_available(), reason="no reachable Postgres (set TEST_DATABASE_URL)")

# How long to wait before concluding that a runner which should be
# blocked really is blocked. Generous: the whole 26-file run takes about
# a second locally, so a runner that was going to proceed would have
# finished several times over.
_BLOCKED_PROBE_SECONDS = 1.5


@contextmanager
def _empty_schema() -> Iterator[str]:
    """A fresh, EMPTY, uniquely-named schema -- deliberately not
    `app.db.testing.migrated_schema`, which hands back an
    already-migrated one. Every test here is about what happens on a
    database that has never been migrated, which is the exact state a
    first deploy and its own cold-start wake are both racing to leave.
    """
    schema = f"conc_{uuid.uuid4().hex[:16]}"
    admin = psycopg.connect(_candidate_dsn(), row_factory=psycopg.rows.dict_row, autocommit=True)
    try:
        admin.execute(f'CREATE SCHEMA "{schema}"')
        yield schema
    finally:
        admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        admin.close()


def _connect(schema: str) -> psycopg.Connection[dict[str, object]]:
    return psycopg.connect(
        _candidate_dsn(),
        row_factory=psycopg.rows.dict_row,
        options=f"-c search_path={schema}",
    )


def _lock_is_free() -> bool:
    """Whether a brand-new session can take the migration lock. Must be a
    SEPARATE session: advisory locks are re-entrant within one session,
    so asking on the connection that took it always says yes.
    """
    probe = psycopg.connect(_candidate_dsn(), row_factory=psycopg.rows.dict_row)
    try:
        row = probe.execute("SELECT pg_try_advisory_lock(%s) AS got", (MIGRATION_ADVISORY_LOCK_KEY,)).fetchone()
        got = bool(row and row["got"])
        if got:
            probe.execute("SELECT pg_advisory_unlock(%s)", (MIGRATION_ADVISORY_LOCK_KEY,))
        return got
    finally:
        probe.close()


def test_run_migrations_waits_while_another_session_holds_the_lock() -> None:
    """The load-bearing assertion, and the one that fails outright
    without the fix: a runner must BLOCK while somebody else is
    migrating, not charge ahead and collide with them.

    A held lock is used rather than a real second runner so the test is
    deterministic -- no reliance on two runs happening to overlap.
    """
    with _empty_schema() as schema:
        holder = psycopg.connect(_candidate_dsn(), row_factory=psycopg.rows.dict_row)
        finished = threading.Event()
        error: list[BaseException] = []

        def _runner() -> None:
            conn = _connect(schema)
            try:
                run_migrations(conn, MIGRATIONS_DIR)
            except BaseException as exc:  # noqa: BLE001 - re-raised in the main thread below
                error.append(exc)
            finally:
                conn.close()
                finished.set()

        try:
            holder.execute("SELECT pg_advisory_lock(%s)", (MIGRATION_ADVISORY_LOCK_KEY,))
            thread = threading.Thread(target=_runner, daemon=True)
            thread.start()

            assert not finished.wait(timeout=_BLOCKED_PROBE_SECONDS), (
                "run_migrations completed while another session held the migration "
                "advisory lock -- it is not taking the lock at all, which is the "
                "state that produced the duplicate-key crash on parallel deploys"
            )
        finally:
            holder.execute("SELECT pg_advisory_unlock(%s)", (MIGRATION_ADVISORY_LOCK_KEY,))
            holder.commit()
            holder.close()

        assert finished.wait(timeout=60), "run_migrations never finished after the lock was released"
        thread.join(timeout=10)
        if error:
            raise error[0]


def test_two_parallel_runners_on_one_fresh_database_both_succeed() -> None:
    """The original crash, reproduced as a test: two runners started at
    the same moment against a database that has never been migrated.

    Before the fix this raised `UniqueViolation` on
    `pg_type_typname_nsp_index` (5/5 with parallel processes); with the
    lock but without the commit-before-release it raised `DuplicateTable:
    relation "settings" already exists` (6/6). Both are the same bug seen
    from different angles, and both are what a Render deploy that
    overlapped a cold-start wake would have printed instead of starting
    uvicorn.
    """
    with _empty_schema() as schema:
        results: list[list[str]] = []
        errors: list[BaseException] = []
        start = threading.Barrier(2)

        def _runner() -> None:
            conn = _connect(schema)
            try:
                start.wait(timeout=30)
                results.append(run_migrations(conn, MIGRATIONS_DIR))
            except BaseException as exc:  # noqa: BLE001 - collected and re-raised below
                errors.append(exc)
            finally:
                conn.close()

        threads = [threading.Thread(target=_runner, daemon=True) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=120)

        if errors:
            raise errors[0]

        assert len(results) == 2
        # Exactly one runner does the work; the other waits and correctly
        # finds nothing left to do. Which one wins is a race and must not
        # be asserted -- that every file is applied exactly once is the
        # actual guarantee.
        assert sorted(len(applied) for applied in results) == [0, len(sorted(MIGRATIONS_DIR.glob("*.sql")))]

        conn = _connect(schema)
        try:
            row = conn.execute(
                "SELECT count(*) AS total, count(DISTINCT filename) AS distinct_files FROM schema_migrations"
            ).fetchone()
        finally:
            conn.close()
        assert row is not None
        assert row["total"] == row["distinct_files"] == len(list(MIGRATIONS_DIR.glob("*.sql")))


def test_lock_is_released_after_a_successful_run() -> None:
    """A session-level advisory lock is NOT released by commit, rollback,
    or the end of a `with` block -- only by an explicit unlock or the
    session ending. `run_migrations` is routinely handed a long-lived
    caller-owned connection (`app.db.testing.migrated_schema`,
    `app.batch.run_daily`), so forgetting the unlock would leave every
    later runner blocked forever rather than merely racing.
    """
    with _empty_schema() as schema:
        conn = _connect(schema)
        try:
            run_migrations(conn, MIGRATIONS_DIR)
            assert _lock_is_free(), "the migration lock is still held after a successful run"
        finally:
            conn.close()


def test_lock_is_released_when_a_migration_raises(tmp_path) -> None:
    """The failure path matters more than the success path here: a broken
    migration already fails the deploy, and a lock leaked on the way out
    would additionally wedge every FUTURE deploy and the daily batch job
    behind a connection nobody is watching.

    The SQL below fails inside its own transaction, which leaves psycopg's
    connection in the aborted-transaction state where Postgres rejects
    every command except ROLLBACK -- including the unlock. That is the
    exact case `_migration_lock`'s `finally` has to recover from, rather
    than the tidy checksum-mismatch failure which leaves a healthy
    connection.
    """
    mig_dir = tmp_path / "migrations"
    mig_dir.mkdir()
    (mig_dir / "0001_broken.sql").write_text("CREATE TABLE broken (id int) WITH (this_is_not_an_option = 1);")

    with _empty_schema() as schema:
        conn = _connect(schema)
        try:
            with pytest.raises(psycopg.Error):
                run_migrations(conn, mig_dir)
            assert _lock_is_free(), "the migration lock leaked when a migration failed"
        finally:
            conn.close()


def test_migrations_are_durable_before_the_lock_is_released() -> None:
    """The half of the fix that is easy to leave out and impossible to
    notice: a lock released before the work it protects is COMMITTED
    protects nothing, because the next waiter reads a database that still
    looks empty.

    Asserted from a SEPARATE connection that never participated in the
    run -- which is exactly the vantage point of the second deploy
    waiting on the lock.
    """
    with _empty_schema() as schema:
        conn = _connect(schema)
        try:
            applied = run_migrations(conn, MIGRATIONS_DIR)
            assert applied, "expected a fresh schema to have migrations to apply"

            observer = _connect(schema)
            try:
                row = observer.execute("SELECT count(*) AS n FROM schema_migrations").fetchone()
            finally:
                observer.close()
            assert row is not None
            assert row["n"] == len(applied), (
                "another session cannot see the applied migrations yet -- they are "
                "still uncommitted, so the next runner would apply them all again"
            )
        finally:
            conn.close()


def test_the_lock_starts_out_free() -> None:
    """Ordering guard: every assertion above about the lock being taken or
    released is only meaningful if the suite did not start with it
    already held by something else.
    """
    assert _lock_is_free()
