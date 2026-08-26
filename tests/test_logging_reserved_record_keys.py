"""`extra={...}` may not reuse a name the stdlib LogRecord already owns,
or the logging call itself raises and takes the caller down with it.

FOUND BY FUZZING, but not through the API — the harness could not even
reach the API, because standing up a migrated schema crashed first:

    File "app/db/migrate.py", line 242, in _apply_migrations
      logger.info("migration_applied", extra={"filename": path.name})
    KeyError: "Attempt to overwrite 'filename' in LogRecord"

`logging.Logger.makeRecord` refuses any `extra` key that collides with
an attribute a LogRecord already defines — `filename`, `module`,
`name`, `args`, `process`, `lineno` and friends — and it signals that
refusal by raising KeyError, not by dropping the field. A log line that
was only ever meant to be observability therefore becomes an exception
on the caller's own code path.

── WHY 1560 TESTS NEVER SAW IT ─────────────────────────────────────────

`Logger.info()` checks `isEnabledFor(INFO)` BEFORE it builds the record,
so at a higher threshold the offending line is never constructed and
never raises. tests/conftest.py sets `LOG_LEVEL=WARNING` for the whole
suite (deliberately, to keep test output readable), which means every
`logger.info(..., extra=...)` in this codebase runs as a no-op under
test and its `extra` dict is never validated. render.yaml sets
`LOG_LEVEL=INFO` in production. The suite was therefore structurally
incapable of catching this class of bug, and the first execution of
that line at INFO would have been on the production deploy itself.

── WHAT IT WOULD HAVE COST ─────────────────────────────────────────────

`app/db/migrate.py` raises AFTER the migration's own transaction has
committed and its row has been inserted into `schema_migrations`. So the
first production deploy would have applied migration 0001 successfully,
recorded it as applied, then died with a KeyError that says nothing
whatsoever about migrations. A retry would skip 0001 as already applied,
apply 0002, and die again — one migration per deploy attempt, each one
looking like a fresh unexplained crash, with the schema left in a
different valid-but-partial state every time. The failure is maximally
confusing precisely because the thing that broke (a log line) is not the
thing that appears to have broken (the migration runner).

`app/services/collector.py`'s collision is on a `logger.warning`, which
IS enabled under test — it survived only because its branch is
unreachable on the current schema (`sources.name` is UNIQUE, so two
unkeyed rows cannot share a name; that function's own docstring says as
much). It is a live landmine for the day that constraint changes, and it
costs one word to defuse.

── THE GUARD ───────────────────────────────────────────────────────────

The static scan below is the real regression test: it fails on ANY
reserved-key collision anywhere under `app/`, including ones added
later, and it does so without needing the offending line to be reachable
or the right log level to be configured. The live migration test beneath
it reproduces the original production crash end-to-end so the fix is
pinned by behaviour and not only by lint.
"""

from __future__ import annotations

import ast
import logging
import uuid
from contextlib import contextmanager
from pathlib import Path

import psycopg
import pytest
from psycopg.rows import dict_row

from app.db.migrate import run_migrations
from app.db.testing import _candidate_dsn, pg_available
from app.logging import configure_logging

APP_DIR = Path(__file__).resolve().parent.parent / "app"

# Every attribute a bare LogRecord defines, plus the two the Formatter
# machinery sets later (`message` via record.getMessage(), `asctime` via
# formatTime) — makeRecord rejects the former outright and the latter
# would be silently overwritten during formatting.
RESERVED_RECORD_KEYS = frozenset(
    vars(logging.LogRecord("", 0, "", 0, "", None, None)).keys()
) | {"message", "asctime"}


def _extra_keys_in_file(path: Path) -> list[tuple[int, str]]:
    """Every literal string key passed to an `extra={...}` keyword in
    this file, with its line number.

    Parsed with `ast` rather than regex so a dict spanning several lines,
    or one containing a nested dict/comprehension, is read correctly —
    a regex over `extra=\\{[^}]*\\}` stops at the first inner brace and
    would quietly miss keys past it.
    """
    found: list[tuple[int, str]] = []
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for kw in node.keywords:
            if kw.arg != "extra" or not isinstance(kw.value, ast.Dict):
                continue
            for key in kw.value.keys:
                if isinstance(key, ast.Constant) and isinstance(key.value, str):
                    found.append((key.lineno, key.value))
    return found


def test_no_logging_extra_collides_with_a_reserved_logrecord_attribute() -> None:
    """The general guard. A collision anywhere under `app/` is a crash
    waiting for the right log level, so this fails the build rather than
    waiting for the branch to be reached in production.
    """
    collisions = []
    for path in sorted(APP_DIR.rglob("*.py")):
        for lineno, key in _extra_keys_in_file(path):
            if key in RESERVED_RECORD_KEYS:
                collisions.append(f"{path.relative_to(APP_DIR.parent)}:{lineno} extra key {key!r}")

    assert not collisions, (
        "logging `extra=` keys collide with reserved LogRecord attributes; "
        "logging.Logger.makeRecord raises KeyError on these as soon as the "
        "level is enabled. Rename the key (e.g. 'filename' -> 'migration_file'):\n  "
        + "\n  ".join(collisions)
    )


def test_reserved_key_really_does_raise() -> None:
    """Pins the mechanism the test above is protecting against, so that
    if a future Python quietly stopped raising, this file would say so
    rather than silently guarding nothing.
    """
    logger = logging.getLogger("test_reserved_key_probe")
    logger.setLevel(logging.INFO)
    with pytest.raises(KeyError, match="Attempt to overwrite"):
        logger.info("probe", extra={"filename": "x"})


# ── the live reproduction of the production crash ─────────────────────


@contextmanager
def _fresh_unmigrated_schema():
    """A uniquely-named schema with NO migrations applied.

    `app.db.testing.migrated_schema` cannot be reused here: it runs the
    migrations itself during setup, and a second `run_migrations` call
    on an already-migrated schema takes the `migrations_up_to_date`
    branch and never reaches the `migration_applied` line under test.
    """
    dsn = _candidate_dsn()
    schema = f"test_{uuid.uuid4().hex[:16]}"
    admin = psycopg.connect(dsn, row_factory=dict_row, autocommit=True)
    try:
        admin.execute(f'CREATE SCHEMA "{schema}"')
        conn = psycopg.connect(dsn, row_factory=dict_row, options=f"-c search_path={schema}")
        try:
            yield conn
        finally:
            conn.close()
    finally:
        admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        admin.close()


@pytest.mark.skipif(not pg_available(), reason="no reachable Postgres (set TEST_DATABASE_URL)")
def test_migrations_run_clean_at_the_production_log_level() -> None:
    """End-to-end reproduction: run the real migration runner at INFO,
    which is what render.yaml configures in production.

    Before the fix this raised `KeyError: "Attempt to overwrite
    'filename' in LogRecord"` on the first migration applied. The
    assertion that `applied` is non-empty matters — it is what proves
    the run actually reached the logging call rather than short-circuiting
    on an already-migrated schema and passing vacuously.
    """
    root_level = logging.getLogger().level
    configure_logging("INFO")
    try:
        with _fresh_unmigrated_schema() as conn:
            applied = run_migrations(conn)
        assert applied, "no migrations were applied, so the logging path was never exercised"
    finally:
        # Restore the suite's own level (conftest sets LOG_LEVEL=WARNING)
        # so this test does not make every later test noisy.
        logging.getLogger().setLevel(root_level)


def test_structured_formatter_survives_the_migration_log_line() -> None:
    """The formatter half of the same path: even once makeRecord accepts
    the key, `StructuredFormatter` has to serialize it. Cheap to assert
    and it covers the `message`/`asctime` half of RESERVED_RECORD_KEYS,
    which makeRecord does NOT reject but formatting would clobber.
    """
    from app.logging import StructuredFormatter

    record = logging.LogRecord("app.db.migrate", logging.INFO, "migrate.py", 242, "migration_applied", None, None)
    record.migration_file = "0003_articles.sql"  # type: ignore[attr-defined]
    rendered = StructuredFormatter().format(record)
    assert "0003_articles.sql" in rendered
    assert "migration_applied" in rendered
