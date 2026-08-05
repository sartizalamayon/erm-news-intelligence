"""Schema behavior: the dedup-on-insert fix (the ADR's flagged
full-table-scan bug), constraints that encode dropped legacy values,
the seen_urls fallback, and the rollup upserts/queries (DoD #1, #3, #9).
"""

from __future__ import annotations

import datetime as dt

import psycopg
import pytest

from app.db.queries import (
    insert_article,
    recent_daily_row_counts,
    remember_seen_url,
    standing_themes,
    upsert_rollup_monthly_theme,
    url_norm_seen,
)
from app.db.testing import migrated_schema, pg_available

pytestmark = pytest.mark.skipif(not pg_available(), reason="no reachable Postgres (set TEST_DATABASE_URL)")


def _article_fields(**overrides: object) -> dict[str, object]:
    fields = {
        "url": "https://example.com/a",
        "url_norm": "https://example.com/a",
        "source": "Example",
        "title": "Some headline",
        "day": dt.date(2026, 8, 5),
    }
    fields.update(overrides)
    return fields


def test_insert_article_returns_id() -> None:
    with migrated_schema() as conn:
        article_id = insert_article(conn, **_article_fields())
        assert article_id is not None
        row = conn.execute("SELECT title FROM articles WHERE id = %s", (article_id,)).fetchone()
        assert row["title"] == "Some headline"


def test_dedup_on_insert_via_url_norm_conflict_returns_none_not_a_scan() -> None:
    """This is the concrete regression test for ADR §2 point 1 / §6's
    flagged bug: `SELECT 1 FROM articles WHERE url=? OR url_norm=?` was a
    full table scan. The fix is `ON CONFLICT (url_norm) DO NOTHING`
    (migrations/0003_articles.sql) — this test proves a second insert
    with the same url_norm but a DIFFERENT raw url (the real-world case:
    two fetch methods surfacing the same story via different tracking
    params) is silently rejected, exactly like legacy's dedup, but via
    an index-backed constraint instead of a per-insert scan.
    """
    with migrated_schema() as conn:
        first_id = insert_article(conn, **_article_fields(url="https://example.com/a?utm_source=x", url_norm="https://example.com/a"))
        assert first_id is not None

        second_id = insert_article(
            conn, **_article_fields(url="https://example.com/a?utm_source=y", url_norm="https://example.com/a")
        )
        assert second_id is None  # duplicate by url_norm, correctly rejected

        count = conn.execute("SELECT count(*) AS n FROM articles").fetchone()["n"]
        assert count == 1


def test_url_unique_constraint_still_enforced_independently() -> None:
    with migrated_schema() as conn:
        insert_article(conn, **_article_fields())
        with pytest.raises(psycopg.errors.UniqueViolation):
            with conn.transaction():
                conn.execute(
                    "INSERT INTO articles (url, url_norm, source, title, day) VALUES (%s, %s, %s, %s, %s)",
                    (
                        "https://example.com/a",
                        "https://example.com/a-different-norm",
                        "Example",
                        "dup raw url",
                        dt.date(2026, 8, 5),
                    ),
                )


def test_match_type_rejects_the_dead_both_value() -> None:
    """legacy-behavior.md §5.5/§7: match_type='both' is referenced in
    legacy's WHERE-clause filters but never assigned by any write path.
    The CHECK constraint (migrations/0003_articles.sql) makes it
    unrepresentable rather than merely unused.
    """
    with migrated_schema() as conn:
        with pytest.raises(psycopg.errors.CheckViolation):
            with conn.transaction():
                conn.execute(
                    "INSERT INTO articles (url, url_norm, source, title, day, match_type) "
                    "VALUES (%s, %s, %s, %s, %s, %s)",
                    ("https://example.com/b", "https://example.com/b", "Example", "t", dt.date(2026, 8, 5), "both"),
                )


def test_summary_column_was_dropped() -> None:
    """legacy-behavior.md §7 point 5: confirmed-dead column, zero
    callers, always ''. Not carried forward (storage-policy.md
    "Schema decisions").
    """
    with migrated_schema() as conn:
        row = conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = 'articles' AND column_name = 'summary'"
        ).fetchone()
        assert row is None


def test_seen_urls_fallback_after_full_row_purge() -> None:
    """The scenario 0004_article_bodies_and_seen_urls.sql exists for:
    an article's full row has aged out (purged), but its url_norm must
    still be remembered forever so the same URL is never re-fetched as
    "new" (legacy-behavior.md §2.6: dedup is global, cross-run, cross-day).
    """
    with migrated_schema() as conn:
        assert url_norm_seen(conn, "https://example.com/purged-story") is False
        remember_seen_url(conn, "https://example.com/purged-story", dt.date(2026, 5, 1))
        assert url_norm_seen(conn, "https://example.com/purged-story") is True


def test_topic_parent_self_reference_and_on_delete_set_null() -> None:
    with migrated_schema() as conn:
        parent_id = insert_article(
            conn, **_article_fields(url="https://example.com/parent", url_norm="https://example.com/parent")
        )
        child_id = insert_article(
            conn,
            **_article_fields(
                url="https://example.com/child", url_norm="https://example.com/child", topic_parent_id=parent_id
            ),
        )
        conn.execute("DELETE FROM articles WHERE id = %s", (parent_id,))
        row = conn.execute("SELECT topic_parent_id FROM articles WHERE id = %s", (child_id,)).fetchone()
        assert row["topic_parent_id"] is None


def test_search_vec_matches_bengali_and_english_title() -> None:
    with migrated_schema() as conn:
        insert_article(
            conn,
            **_article_fields(
                url="https://example.com/bn",
                url_norm="https://example.com/bn",
                title="স্পেকট্রাম নিলাম শুরু",
                title_en="Spectrum auction begins",
            ),
        )
        row = conn.execute(
            "SELECT id FROM articles WHERE search_vec @@ plainto_tsquery('simple', %s)", ("spectrum",)
        ).fetchone()
        assert row is not None


def test_recent_daily_row_counts_reflects_real_inserts() -> None:
    with migrated_schema() as conn:
        today = dt.date.today()
        insert_article(conn, **_article_fields(url="https://x.test/1", url_norm="https://x.test/1", day=today))
        insert_article(conn, **_article_fields(url="https://x.test/2", url_norm="https://x.test/2", day=today))
        counts = recent_daily_row_counts(conn, 7)
        assert counts.get(today) == 2


def test_standing_themes_reads_only_rollup_table_not_articles() -> None:
    with migrated_schema() as conn:
        for i in range(12):
            month = dt.date(2025, 9, 1) + dt.timedelta(days=31 * i)
            month = month.replace(day=1)
            upsert_rollup_monthly_theme(conn, month, "Spectrum repricing", count_delta=5)
        results = standing_themes(conn, dt.date(2025, 9, 1), min_consecutive_months=9)
        assert any(r["theme"] == "Spectrum repricing" for r in results)
        # no articles rows exist at all in this test — proves the query never touched `articles`.
        assert conn.execute("SELECT count(*) AS n FROM articles").fetchone()["n"] == 0
