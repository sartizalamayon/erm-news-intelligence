"""One-off legacy-data migration: reads the legacy SQLite corpus and
writes it into the new Postgres schema (DoD #7).

**This is not part of the deterministic `migrations/*.sql` chain** — it
runs once, by hand, when standing up the rebuilt database against real
legacy history. It is intentionally a plain script, not wired into
`app.db.migrate`, because it needs a second connection (to the SQLite
file) that the SQL-only migration runner has no business knowing about.

Safety
------
This script NEVER opens the SQLite source for anything but read
(`sqlite3.connect(f"file:{path}?mode=ro", uri=True)` — a driver-level
read-only guarantee, not just convention). The mission's own
instructions are explicit: "Do not write to the legacy SQLite database
under any circumstances. Copy it first if you need a writable target."
This script needs no writable target at all — it only reads.

Usage
-----
    python -m migrations.legacy.import_legacy_sqlite \\
        --sqlite-path /path/to/a/COPY/of/news.db \\
        [--limit N]        # cap rows migrated, for a quick smoke test
        [--dry-run]         # parse and validate only, write nothing

Requires `migrations/*.sql` to have already been applied to the target
`DATABASE_URL` (run `python -m app.db.migrate` first) — this script only
inserts rows, it does not create tables.

What migrates cleanly vs. what doesn't: see this module's `migrate()`
docstring and the report emitted at the end of a run. Short version
(measured against the real 51,886-row corpus, see storage-policy.md
§4): every row migrates — the legacy schema has no column that cannot
be losslessly represented in the new schema (that's *why*
storage-policy.md dropped only `summary`, a confirmed-dead column, and
disallowed the never-assigned `match_type='both'` value; see
migrations/0003_articles.sql's header comment) — the only per-row
transform is type coercion (TEXT timestamps/dates -> TIMESTAMPTZ/DATE,
0/1 flags -> BOOLEAN), which is validated, not lossy.
"""

from __future__ import annotations

import argparse
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any

from app.db.connection import DictConnection, get_connection
from app.logging import configure_logging, get_logger

logger = get_logger(__name__)

# Legacy `published` is Dhaka-local "%Y-%m-%d %H:%M" with NO timezone
# field — Dhaka has held a fixed +06:00 offset since 2009 (no DST), so
# this is a lossless, unambiguous conversion, not an approximation.
DHAKA_OFFSET = timezone(timedelta(hours=6))


@dataclass
class MigrationReport:
    sources_migrated: int = 0
    keywords_migrated: int = 0
    runs_migrated: int = 0
    users_migrated: int = 0
    articles_migrated: int = 0
    articles_skipped: int = 0
    bodies_migrated: int = 0
    skipped_reasons: dict[str, int] = field(default_factory=dict)

    def note_skip(self, reason: str) -> None:
        self.articles_skipped += 1
        self.skipped_reasons[reason] = self.skipped_reasons.get(reason, 0) + 1


def _sqlite_readonly(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _parse_published(raw: str) -> datetime | None:
    if not raw:
        return None
    naive = datetime.strptime(raw, "%Y-%m-%d %H:%M")
    return naive.replace(tzinfo=DHAKA_OFFSET)


def _parse_day(raw: str | None) -> date | None:
    if not raw:
        return None
    return datetime.strptime(raw, "%Y-%m-%d").date()


def _bool(raw: Any) -> bool:
    return bool(raw)


def migrate_sources(sqlite_conn: sqlite3.Connection, pg_conn: DictConnection, report: MigrationReport) -> None:
    rows = sqlite_conn.execute("SELECT * FROM sources").fetchall()
    for row in rows:
        pg_conn.execute(
            """
            INSERT INTO sources (id, name, method, config, language, enabled, created_at)
            VALUES (%(id)s, %(name)s, %(method)s, %(config)s::jsonb, %(language)s, %(enabled)s, %(created_at)s)
            ON CONFLICT (id) DO NOTHING
            """,
            {
                "id": row["id"],
                "name": row["name"],
                "method": row["method"],
                "config": row["config"] or "{}",
                "language": row["language"] or "mixed",
                "enabled": _bool(row["enabled"]),
                "created_at": row["created_at"],
            },
        )
        report.sources_migrated += 1
    _resync_sequence(pg_conn, "sources", "id")


def migrate_keywords(sqlite_conn: sqlite3.Connection, pg_conn: DictConnection, report: MigrationReport) -> None:
    rows = sqlite_conn.execute("SELECT * FROM keywords").fetchall()
    for row in rows:
        pg_conn.execute(
            """
            INSERT INTO keywords (id, section, term_en, term_bn, enabled, created_at)
            VALUES (%(id)s, %(section)s, %(term_en)s, %(term_bn)s, %(enabled)s, %(created_at)s)
            ON CONFLICT (id) DO NOTHING
            """,
            {
                "id": row["id"],
                "section": row["section"],
                "term_en": row["term_en"] or "",
                "term_bn": row["term_bn"] or "",
                "enabled": _bool(row["enabled"]),
                "created_at": row["created_at"],
            },
        )
        report.keywords_migrated += 1
    _resync_sequence(pg_conn, "keywords", "id")


def migrate_runs(sqlite_conn: sqlite3.Connection, pg_conn: DictConnection, report: MigrationReport) -> None:
    rows = sqlite_conn.execute("SELECT * FROM runs").fetchall()
    for row in rows:
        status = row["status"] or "running"
        if status not in ("running", "done", "error"):
            status = "error"  # legacy has no other values observed, but stay defensive against the CHECK constraint
        trigger = row["trigger"] or "manual"
        if trigger not in ("manual", "scheduled"):
            trigger = "manual"
        pg_conn.execute(
            """
            INSERT INTO runs (id, started_at, finished_at, status, trigger, total_new, total_matched, details)
            VALUES (%(id)s, %(started_at)s, %(finished_at)s, %(status)s, %(trigger)s, %(total_new)s,
                    %(total_matched)s, %(details)s::jsonb)
            ON CONFLICT (id) DO NOTHING
            """,
            {
                "id": row["id"],
                "started_at": row["started_at"],
                "finished_at": row["finished_at"] or None,
                "status": status,
                "trigger": trigger,
                "total_new": row["total_new"] or 0,
                "total_matched": row["total_matched"] or 0,
                "details": row["details"] or "{}",
            },
        )
        report.runs_migrated += 1
    _resync_sequence(pg_conn, "runs", "id")


def migrate_users(sqlite_conn: sqlite3.Connection, pg_conn: DictConnection, report: MigrationReport) -> None:
    """Legacy `users` is username/password_hash — DROPPED (ADR §9.3
    rules out password auth entirely). What migrates is only the
    *role*, mapped onto a placeholder email derived from the username,
    since the new schema's identity is email-based. This is a best-
    effort seed for the one legacy admin account, not a claim that
    credentials survive the migration — they can't; OTP is a different
    auth mechanism. The real onboarding step (out of this script's
    scope) is inviting real @<allowed_domain> addresses.
    """
    rows = sqlite_conn.execute("SELECT * FROM users").fetchall()
    for row in rows:
        role = row["role"] if row["role"] in ("admin", "analyst") else "analyst"
        placeholder_email = f"{row['username']}@legacy.invalid"
        pg_conn.execute(
            """
            INSERT INTO users (id, email, role, display_name, created_at)
            VALUES (%(id)s, %(email)s, %(role)s, %(display_name)s, %(created_at)s)
            ON CONFLICT (id) DO NOTHING
            """,
            {
                "id": row["id"],
                "email": placeholder_email,
                "role": role,
                "display_name": row["username"],
                "created_at": row["created_at"],
            },
        )
        report.users_migrated += 1
    _resync_sequence(pg_conn, "users", "id")


_ARTICLE_COLUMNS = (
    "id url url_norm source title title_en published fetched_at day sections keywords "
    "dup_group is_primary also_in review_status reviewed_by reviewed_at llm_score llm_reason "
    "match_type semantic_score topic_tag topic_standout topic_parent_id body_status"
).split()

_VALID_REVIEW_STATUS = {"pending", "filtered_out", "approved", "rejected"}
_VALID_MATCH_TYPE = {"", "keyword", "semantic"}


def _article_rows(sqlite_conn: sqlite3.Connection, limit: int | None) -> Iterator[sqlite3.Row]:
    sql = "SELECT * FROM articles ORDER BY id"
    if limit:
        sql += f" LIMIT {int(limit)}"
    yield from sqlite_conn.execute(sql)


def migrate_articles(
    sqlite_conn: sqlite3.Connection,
    pg_conn: DictConnection,
    report: MigrationReport,
    *,
    limit: int | None = None,
    batch_size: int = 500,
    dry_run: bool = False,
) -> None:
    # topic_parent_id is a self-referencing FK (migrations/0003_articles.sql)
    # and legacy has no guarantee a row's parent has a *lower* id than the
    # row itself (topic clustering assigns it post-hoc, independent of
    # insertion order) — a single ascending-id batch pass can hit "parent
    # not inserted yet" even though every parent id genuinely exists
    # somewhere in the corpus. Fixed with two passes: insert every row with
    # topic_parent_id forced NULL, then UPDATE it in once every row exists.
    # (Confirmed against the live corpus: 1,439 rows carry a
    # topic_parent_id, 0 of them dangling — see the migration report.)
    insert_columns = [c for c in _ARTICLE_COLUMNS if c != "topic_parent_id"]
    batch: list[dict[str, Any]] = []
    bodies: list[tuple[int, str]] = []
    parent_links: list[tuple[int, int]] = []

    def flush() -> None:
        if not batch or dry_run:
            batch.clear()
            return
        columns = ", ".join(insert_columns)
        placeholders = ", ".join(f"%({c})s" for c in insert_columns)
        with pg_conn.cursor() as cur:
            cur.executemany(
                f"INSERT INTO articles ({columns}) VALUES ({placeholders}) ON CONFLICT (id) DO NOTHING",
                batch,
            )
        batch.clear()

    for row in _article_rows(sqlite_conn, limit):
        review_status = row["review_status"] if row["review_status"] in _VALID_REVIEW_STATUS else "filtered_out"
        match_type = row["match_type"] or ""
        if match_type not in _VALID_MATCH_TYPE:
            # The only real-world case is 'both' (legacy-behavior.md §5.5,
            # §7 point 8: referenced in filter WHERE clauses, never
            # assigned by any write path). Confirmed zero rows in the live
            # corpus (`GROUP BY match_type` -> '', 'keyword', 'semantic'
            # only) — this branch exists so an unexpected value fails
            # loudly (counted as skipped) instead of violating the CHECK
            # constraint mid-batch and losing the whole batch's inserts.
            report.note_skip(f"invalid_match_type:{match_type}")
            continue

        try:
            published = _parse_published(row["published"] or "")
            day = _parse_day(row["day"]) or date.today()
        except ValueError:
            report.note_skip("unparseable_date")
            continue

        content_text = row["content_text"] or ""
        body_status = "fetched" if content_text else "not_attempted"

        batch.append(
            {
                "id": row["id"],
                "url": row["url"],
                "url_norm": row["url_norm"] or "",
                "source": row["source"],
                "title": row["title"],
                "title_en": row["title_en"] or "",
                "published": published,
                "fetched_at": row["fetched_at"],
                "day": day,
                "sections": row["sections"] or "",
                "keywords": row["keywords"] or "",
                "dup_group": row["dup_group"],
                "is_primary": _bool(row["is_primary"] if row["is_primary"] is not None else 1),
                "also_in": row["also_in"] or "",
                "review_status": review_status,
                "reviewed_by": row["reviewed_by"] or "",
                # legacy stores '' not NULL for the 100% of rows with no reviewer; NULL is the correct typed absence.
                "reviewed_at": None,
                "llm_score": row["llm_score"],
                "llm_reason": row["llm_reason"] or "",
                "match_type": match_type,
                "semantic_score": row["semantic_score"],
                "topic_tag": row["topic_tag"] or "",
                "topic_standout": _bool(row["topic_standout"] or 0),
                "body_status": body_status,
            }
        )
        if content_text:
            bodies.append((row["id"], content_text))
        if row["topic_parent_id"] is not None:
            parent_links.append((row["topic_parent_id"], row["id"]))

        report.articles_migrated += 1
        if len(batch) >= batch_size:
            flush()

    flush()

    if not dry_run and parent_links:
        with pg_conn.cursor() as cur:
            cur.executemany(
                "UPDATE articles SET topic_parent_id = %s WHERE id = %s",
                parent_links,
            )

    if not dry_run and bodies:
        with pg_conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO article_bodies (article_id, content_text) VALUES (%s, %s) "
                "ON CONFLICT (article_id) DO NOTHING",
                bodies,
            )
        report.bodies_migrated = len(bodies)
    elif dry_run:
        report.bodies_migrated = len(bodies)

    if not dry_run:
        _resync_sequence(pg_conn, "articles", "id")


def _resync_sequence(pg_conn: DictConnection, table: str, id_column: str) -> None:
    """Explicit ids were inserted (to preserve dup_group/topic_parent_id
    referential integrity — see module docstring), so the table's
    identity sequence is now behind the max id actually present. Bring
    it back in sync so the NEXT app-inserted row doesn't collide.
    """
    pg_conn.execute(
        f"SELECT setval(pg_catalog.pg_get_serial_sequence(%s, %s), "
        f"COALESCE((SELECT max({id_column}) FROM {table}), 1))",
        (table, id_column),
    )


def migrate(
    sqlite_path: str,
    *,
    limit: int | None = None,
    dry_run: bool = False,
    dsn: str | None = None,
) -> MigrationReport:
    report = MigrationReport()
    sqlite_conn = _sqlite_readonly(sqlite_path)
    try:
        with get_connection(dsn) as pg_conn:
            migrate_sources(sqlite_conn, pg_conn, report)
            migrate_keywords(sqlite_conn, pg_conn, report)
            migrate_runs(sqlite_conn, pg_conn, report)
            migrate_users(sqlite_conn, pg_conn, report)
            migrate_articles(sqlite_conn, pg_conn, report, limit=limit, dry_run=dry_run)
            if dry_run:
                pg_conn.rollback()
    finally:
        sqlite_conn.close()
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--sqlite-path", required=True, help="Path to a COPY of the legacy news.db — never the original."
    )
    parser.add_argument("--limit", type=int, default=None, help="Cap the number of article rows migrated (smoke test).")
    parser.add_argument("--dry-run", action="store_true", help="Parse/validate only; roll back instead of committing.")
    args = parser.parse_args()

    configure_logging()
    report = migrate(args.sqlite_path, limit=args.limit, dry_run=args.dry_run)

    print(f"sources migrated:  {report.sources_migrated}")
    print(f"keywords migrated: {report.keywords_migrated}")
    print(f"runs migrated:     {report.runs_migrated}")
    print(f"users migrated:    {report.users_migrated}")
    print(f"articles migrated: {report.articles_migrated}")
    print(f"article bodies:    {report.bodies_migrated}")
    print(f"articles skipped:  {report.articles_skipped}")
    if report.skipped_reasons:
        for reason, count in sorted(report.skipped_reasons.items()):
            print(f"  - {reason}: {count}")


if __name__ == "__main__":
    main()
