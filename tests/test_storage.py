"""Storage accounting and purge (DoD #4, #5): real Postgres
introspection, the ceiling as configuration, dry-run vs. confirmed
purges, and the export-first gate.
"""

from __future__ import annotations

import datetime as dt

import pytest

from app.db.queries import insert_article
from app.db.settings import set_setting
from app.db.testing import migrated_schema, pg_available
from app.services.storage import (
    get_storage_usage,
    purge_matched_articles,
    purge_stale_bodies,
    purge_stale_unmatched_articles,
)

pytestmark = pytest.mark.skipif(not pg_available(), reason="no reachable Postgres (set TEST_DATABASE_URL)")


def _article(**overrides: object) -> dict[str, object]:
    fields = {
        "url": "https://example.com/x",
        "url_norm": "https://example.com/x",
        "source": "Example",
        "title": "Headline",
        "day": dt.date.today(),
        "review_status": "pending",
    }
    fields.update(overrides)
    return fields


def test_storage_usage_reflects_real_introspection_and_configured_ceiling() -> None:
    with migrated_schema() as conn:
        insert_article(conn, **_article(url="https://example.com/1", url_norm="https://example.com/1"))
        conn.commit()

        usage = get_storage_usage(conn)
        assert usage.database_size_bytes > 0
        assert usage.ceiling_bytes == 500 * 1024 * 1024  # the seeded default
        assert "articles" in usage.table_sizes_bytes
        assert usage.table_sizes_bytes["articles"] > 0
        assert 0 <= usage.pct_used < 100

        set_setting(conn, "storage_ceiling_mb", "1")
        conn.commit()
        usage_tiny_ceiling = get_storage_usage(conn)
        assert usage_tiny_ceiling.ceiling_bytes == 1 * 1024 * 1024
        assert usage_tiny_ceiling.pct_used > usage.pct_used


def test_purge_stale_bodies_dry_run_does_not_delete() -> None:
    with migrated_schema() as conn:
        article_id = insert_article(conn, **_article())
        conn.execute(
            "INSERT INTO article_bodies (article_id, content_text, fetched_at) "
            "VALUES (%s, %s, now() - interval '400 days')",
            (article_id, "some body text"),
        )
        conn.commit()

        preview = purge_stale_bodies(conn, older_than_days=60, confirm=False)
        assert preview.confirmed is False
        assert preview.rows_affected == 1

        remaining = conn.execute("SELECT count(*) AS n FROM article_bodies").fetchone()["n"]
        assert remaining == 1  # dry run must not mutate


def test_purge_stale_bodies_confirmed_deletes_and_marks_purged() -> None:
    with migrated_schema() as conn:
        article_id = insert_article(conn, **_article())
        conn.execute(
            "INSERT INTO article_bodies (article_id, content_text, fetched_at) "
            "VALUES (%s, %s, now() - interval '400 days')",
            (article_id, "old body"),
        )
        conn.commit()

        result = purge_stale_bodies(conn, older_than_days=60, confirm=True)
        conn.commit()
        assert result.confirmed is True
        assert result.rows_affected == 1

        remaining = conn.execute("SELECT count(*) AS n FROM article_bodies").fetchone()["n"]
        assert remaining == 0
        status = conn.execute("SELECT body_status FROM articles WHERE id = %s", (article_id,)).fetchone()
        assert status["body_status"] == "purged"


def test_purge_stale_unmatched_articles_preserves_dedup_via_seen_urls() -> None:
    with migrated_schema() as conn:
        old_day = dt.date.today() - dt.timedelta(days=120)
        article_id = insert_article(
            conn,
            **_article(
                url="https://example.com/old-unmatched",
                url_norm="https://example.com/old-unmatched",
                day=old_day,
                review_status="filtered_out",
            ),
        )
        assert article_id is not None
        conn.commit()

        result = purge_stale_unmatched_articles(conn, older_than_days=90, confirm=True)
        conn.commit()
        assert result.rows_affected == 1

        gone = conn.execute("SELECT 1 FROM articles WHERE id = %s", (article_id,)).fetchone()
        assert gone is None
        remembered = conn.execute(
            "SELECT 1 FROM seen_urls WHERE url_norm = %s", ("https://example.com/old-unmatched",)
        ).fetchone()
        assert remembered is not None


def test_purge_stale_unmatched_articles_never_touches_pending_or_approved() -> None:
    with migrated_schema() as conn:
        old_day = dt.date.today() - dt.timedelta(days=120)
        pending_id = insert_article(
            conn,
            **_article(
                url="https://x.test/still-pending",
                url_norm="https://x.test/still-pending",
                day=old_day,
                review_status="pending",
            ),
        )
        conn.commit()

        purge_stale_unmatched_articles(conn, older_than_days=90, confirm=True)
        conn.commit()

        still_there = conn.execute("SELECT 1 FROM articles WHERE id = %s", (pending_id,)).fetchone()
        assert still_there is not None


def test_purge_matched_articles_require_exported_blocks_unexported_rows() -> None:
    with migrated_schema() as conn:
        old_day = dt.date.today() - dt.timedelta(days=400)
        insert_article(
            conn, **_article(url="https://x.test/never-exported", url_norm="https://x.test/never-exported", day=old_day)
        )
        conn.commit()

        preview = purge_matched_articles(conn, older_than=dt.date.today(), require_exported=True, confirm=False)
        assert preview.rows_affected == 0  # nothing eligible -- never exported

        result = purge_matched_articles(conn, older_than=dt.date.today(), require_exported=True, confirm=True)
        conn.commit()
        assert result.rows_affected == 0
        still_there = conn.execute("SELECT count(*) AS n FROM articles").fetchone()["n"]
        assert still_there == 1


def test_purge_matched_articles_allows_exported_rows() -> None:
    with migrated_schema() as conn:
        old_day = dt.date.today() - dt.timedelta(days=400)
        article_id = insert_article(
            conn, **_article(url="https://x.test/exported", url_norm="https://x.test/exported", day=old_day)
        )
        conn.execute("UPDATE articles SET exported_at = now() WHERE id = %s", (article_id,))
        conn.commit()

        result = purge_matched_articles(conn, older_than=dt.date.today(), require_exported=True, confirm=True)
        conn.commit()
        assert result.rows_affected == 1
        assert conn.execute("SELECT count(*) AS n FROM articles").fetchone()["n"] == 0
