"""Migration runner: applies deterministically from empty, is idempotent,
and rejects a tampered-with already-applied file (DoD #2, #9).
"""

from __future__ import annotations

import pytest

from app.db.migrate import MIGRATIONS_DIR, run_migrations
from app.db.testing import migrated_schema, pg_available

pytestmark = pytest.mark.skipif(not pg_available(), reason="no reachable Postgres (set TEST_DATABASE_URL)")


def test_runs_forward_from_empty_and_creates_expected_tables() -> None:
    with migrated_schema() as conn:
        rows = conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema()"
        ).fetchall()
        table_names = {row["table_name"] for row in rows}

        expected = {
            "settings",
            "sources",
            "keywords",
            "runs",
            "articles",
            "article_bodies",
            "seen_urls",
            "users",
            "sessions",
            "otp_codes",
            "provider_configs",
            "rollup_monthly_section",
            "rollup_monthly_source",
            "rollup_monthly_score_band",
            "rollup_monthly_theme",
            "macro_indicator_values",
            "schema_migrations",
        }
        assert expected <= table_names


def test_second_run_is_a_no_op() -> None:
    with migrated_schema() as conn:
        applied_again = run_migrations(conn, MIGRATIONS_DIR)
        assert applied_again == []


def test_seeded_settings_defaults_present() -> None:
    with migrated_schema() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = 'storage_ceiling_mb'").fetchone()
        assert row is not None
        assert row["value"] == "500"


def test_tampered_migration_file_raises(tmp_path) -> None:
    # A standalone migrations dir with one file, applied once, then
    # edited on disk — the runner must refuse to silently re-apply or
    # ignore it (see app/db/migrate.py's module docstring).
    mig_dir = tmp_path / "migrations"
    mig_dir.mkdir()
    (mig_dir / "0001_x.sql").write_text("CREATE TABLE x (id int);")

    with migrated_schema() as conn:
        # Layer this scratch migration on top of the real schema in the same throwaway schema.
        run_migrations(conn, mig_dir)
        (mig_dir / "0001_x.sql").write_text("CREATE TABLE x (id int, extra int);")
        with pytest.raises(RuntimeError, match="checksum mismatch"):
            run_migrations(conn, mig_dir)
