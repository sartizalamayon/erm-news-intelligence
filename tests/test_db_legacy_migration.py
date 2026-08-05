"""Legacy SQLite -> Postgres migration (DoD #7): exercises the real
migration functions from `migrations/legacy/import_legacy_sqlite.py`
against a small synthetic legacy-shaped SQLite database covering the
edge cases that matter — an undated article, a forward-referencing
`topic_parent_id`, the dead `match_type='both'` value, and a body-bearing
row that must land in `article_bodies` — rather than depending on the
real 76MB corpus being present in CI.

The real 51,886-row corpus WAS migrated end-to-end against a throwaway
local Postgres as part of building this feature (never against the
original `data/news.db`, only a `cp`'d copy per the mission's explicit
instruction) — see storage-policy.md §4 for the measured results
(0 rows skipped, real Postgres sizes). This test is the fast,
CI-shaped regression check for the same code path.
"""

from __future__ import annotations

import sqlite3

import pytest

from app.db.testing import migrated_schema, pg_available
from migrations.legacy.import_legacy_sqlite import (
    MigrationReport,
    migrate_articles,
    migrate_keywords,
    migrate_runs,
    migrate_sources,
    migrate_users,
)

pytestmark = pytest.mark.skipif(not pg_available(), reason="no reachable Postgres (set TEST_DATABASE_URL)")

_LEGACY_SCHEMA = """
CREATE TABLE sources (id INTEGER PRIMARY KEY, name TEXT UNIQUE, method TEXT, config TEXT DEFAULT '{}',
    language TEXT DEFAULT 'mixed', enabled INTEGER DEFAULT 1, created_at TEXT DEFAULT (datetime('now')));
CREATE TABLE keywords (id INTEGER PRIMARY KEY, section TEXT, term_en TEXT DEFAULT '', term_bn TEXT DEFAULT '',
    enabled INTEGER DEFAULT 1, created_at TEXT DEFAULT (datetime('now')));
CREATE TABLE runs (id INTEGER PRIMARY KEY, started_at TEXT, finished_at TEXT DEFAULT '', status TEXT DEFAULT 'running',
    trigger TEXT DEFAULT 'manual', total_new INTEGER DEFAULT 0, total_matched INTEGER DEFAULT 0,
    details TEXT DEFAULT '{}');
CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT UNIQUE, password_hash TEXT, role TEXT DEFAULT 'reviewer',
    created_at TEXT DEFAULT (datetime('now')));
CREATE TABLE articles (
    id INTEGER PRIMARY KEY, url TEXT UNIQUE, source TEXT, title TEXT, published TEXT DEFAULT '',
    fetched_at TEXT DEFAULT (datetime('now')), day TEXT, sections TEXT DEFAULT '', keywords TEXT DEFAULT '',
    dup_group INTEGER, is_primary INTEGER DEFAULT 1, also_in TEXT DEFAULT '', review_status TEXT DEFAULT 'pending',
    reviewed_by TEXT DEFAULT '', reviewed_at TEXT DEFAULT '', llm_score REAL, llm_reason TEXT DEFAULT '',
    summary TEXT DEFAULT '', url_norm TEXT DEFAULT '', title_en TEXT DEFAULT '', content_text TEXT DEFAULT '',
    match_type TEXT DEFAULT '', semantic_score REAL, topic_tag TEXT DEFAULT '', topic_standout INTEGER DEFAULT 0,
    topic_parent_id INTEGER
);
"""


def _build_legacy_sqlite(path: str) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(_LEGACY_SCHEMA)
    conn.execute(
        "INSERT INTO sources (id, name, method, config, language, enabled) VALUES (1,'Test Source','rss','{}','en',1)"
    )
    conn.execute(
        "INSERT INTO keywords (id, section, term_en, term_bn, enabled) "
        "VALUES (1,'Geopolitical','spectrum','স্পেকট্রাম',1)"
    )
    conn.execute(
        "INSERT INTO runs (id, started_at, finished_at, status, trigger, total_new, total_matched, details) "
        "VALUES (1,'2026-08-05 09:00','2026-08-05 09:10','done','scheduled',3,2,'{}')"
    )
    conn.execute("INSERT INTO users (id, username, password_hash, role) VALUES (1,'admin','hash','admin')")

    # Row A: normal matched article with a body, dated.
    conn.execute(
        "INSERT INTO articles (id, url, source, title, published, day, sections, keywords, review_status, "
        "match_type, content_text, topic_parent_id) VALUES "
        "(100, 'https://x.test/a', 'Test Source', 'Headline A', '2026-08-05 08:42', '2026-08-05', "
        "'Geopolitical', 'spectrum', 'pending', 'keyword', 'Full article body text.', NULL)"
    )
    # Row B: undated (published='') filtered-out article.
    conn.execute(
        "INSERT INTO articles (id, url, source, title, published, day, review_status, match_type) VALUES "
        "(101, 'https://x.test/b', 'Test Source', 'Headline B', '', '2026-08-05', 'filtered_out', '')"
    )
    # Row C: topic_parent_id forward-references row D (id 103 > 102), the ordering edge case.
    conn.execute(
        "INSERT INTO articles (id, url, source, title, day, review_status, match_type, topic_parent_id) VALUES "
        "(102, 'https://x.test/c', 'Test Source', 'Headline C', '2026-08-05', 'pending', 'keyword', 103)"
    )
    conn.execute(
        "INSERT INTO articles (id, url, source, title, day, review_status, match_type) VALUES "
        "(103, 'https://x.test/d', 'Test Source', 'Headline D', '2026-08-05', 'pending', 'keyword')"
    )
    # Row E: the dead match_type='both' value -- must be skipped, not crash the batch.
    conn.execute(
        "INSERT INTO articles (id, url, source, title, day, review_status, match_type) VALUES "
        "(104, 'https://x.test/e', 'Test Source', 'Headline E', '2026-08-05', 'pending', 'both')"
    )
    conn.commit()
    conn.close()


def test_legacy_migration_end_to_end(tmp_path) -> None:
    sqlite_path = str(tmp_path / "legacy_copy.db")
    _build_legacy_sqlite(sqlite_path)

    sqlite_conn = sqlite3.connect(sqlite_path)
    sqlite_conn.row_factory = sqlite3.Row

    with migrated_schema() as pg_conn:
        report = MigrationReport()
        migrate_sources(sqlite_conn, pg_conn, report)
        migrate_keywords(sqlite_conn, pg_conn, report)
        migrate_runs(sqlite_conn, pg_conn, report)
        migrate_users(sqlite_conn, pg_conn, report)
        migrate_articles(sqlite_conn, pg_conn, report)

        assert report.sources_migrated == 1
        assert report.keywords_migrated == 1
        assert report.runs_migrated == 1
        assert report.users_migrated == 1
        assert report.articles_migrated == 4  # 5 source rows, 1 skipped (match_type='both')
        assert report.articles_skipped == 1
        assert report.skipped_reasons == {"invalid_match_type:both": 1}
        assert report.bodies_migrated == 1

        # Row A: body landed in article_bodies, dated published parsed to Dhaka-offset timestamptz.
        row_a = pg_conn.execute("SELECT published, review_status, body_status FROM articles WHERE id = 100").fetchone()
        assert row_a["review_status"] == "pending"
        assert row_a["body_status"] == "fetched"
        assert row_a["published"].utcoffset().total_seconds() == 6 * 3600
        body = pg_conn.execute("SELECT content_text FROM article_bodies WHERE article_id = 100").fetchone()
        assert body["content_text"] == "Full article body text."

        # Row B: undated -> NULL published, not a crash.
        row_b = pg_conn.execute("SELECT published FROM articles WHERE id = 101").fetchone()
        assert row_b["published"] is None

        # Row C/D: forward-referencing topic_parent_id resolved by the two-pass update.
        row_c = pg_conn.execute("SELECT topic_parent_id FROM articles WHERE id = 102").fetchone()
        assert row_c["topic_parent_id"] == 103

        # Row E (match_type='both') never made it into Postgres at all.
        row_e = pg_conn.execute("SELECT 1 FROM articles WHERE id = 104").fetchone()
        assert row_e is None

    sqlite_conn.close()
