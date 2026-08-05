"""Storage accounting and purge (DoD #4, #5).

Backs the admin dashboard's storage panel: current usage, the ceiling
(configuration, not a constant — `settings.storage_ceiling_mb`,
`app/db/settings.py`), percent used, and a projected days-remaining
figure computed from real Postgres introspection (`pg_database_size`,
`pg_total_relation_size`) and the corpus's own recent insert rate — no
hardcoded growth-rate constant.

Purge is manual and admin-triggered (never automatic for the
high-value, indefinitely-retained "matched article" tier — see
storage-policy.md §2) with a dry-run count and an explicit confirmation
step: every purge function defaults to `confirm=False` (preview only,
mutates nothing) and only deletes when the caller passes
`confirm=True`. `require_exported=True` enforces DoD #5's "purge must
never delete anything not yet exported if the operator asked for
export-first" by filtering on `articles.exported_at`, set by
`app.services.data_transfer.export_to_jsonl` whenever a row is
included in a completed export.

The two "hygiene" purges (shedding article bodies, compacting stale
unmatched rows into `seen_urls`) are what keeps the database's
unbounded growth rate down to the number computed in storage-policy.md
§3 — they are safe to run routinely (e.g. daily, from the batch job)
since neither touches reviewed/exported content. The third purge
(matched/reviewed articles) is the deliberate "release valve" ADR §9.2
describes and is meant to be admin-invoked occasionally, not scheduled.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from app.db.connection import DictConnection
from app.db.queries import database_size_bytes, recent_daily_row_counts, table_sizes_bytes
from app.db.settings import get_setting_int
from app.errors import ValidationError

MiB = 1024 * 1024


@dataclass
class StorageUsage:
    database_size_bytes: int
    ceiling_bytes: int
    pct_used: float
    table_sizes_bytes: dict[str, int]
    daily_growth_bytes: float | None  # None if there isn't enough recent history to estimate from
    projected_days_remaining: float | None  # None if daily_growth_bytes is None or <= 0 (not growing / shrinking)


@dataclass
class PurgeResult:
    operation: str
    confirmed: bool
    rows_affected: int  # would-affect count when confirmed=False, actually-affected count when confirmed=True
    detail: str = ""


def get_storage_usage(conn: DictConnection, *, growth_sample_days: int = 14, schema: str | None = None) -> StorageUsage:
    """Real introspection, not estimates: `pg_database_size` for the
    total, `pg_total_relation_size` per table (app/db/queries.py). The
    growth rate driving `projected_days_remaining` is measured from the
    last `growth_sample_days` days of actual `articles.day` insert
    counts (app/db/queries.recent_daily_row_counts), multiplied by the
    real measured average row costs — not a constant lifted from a
    one-time analysis. If fewer than 2 days of history exist yet, no
    projection is offered (None) rather than a misleading number from
    too small a sample.
    """
    ceiling_mb = get_setting_int(conn, "storage_ceiling_mb", 500)
    ceiling_bytes = ceiling_mb * MiB
    db_bytes = database_size_bytes(conn)
    sizes = table_sizes_bytes(conn, schema=schema)

    daily_counts = recent_daily_row_counts(conn, growth_sample_days)
    daily_growth_bytes: float | None = None
    projected_days_remaining: float | None = None
    if len(daily_counts) >= 2:
        avg_rows_per_day = sum(daily_counts.values()) / len(daily_counts)
        articles_bytes = sizes.get("articles", 0)
        article_count_row = conn.execute("SELECT count(*) AS n FROM articles").fetchone()
        article_count = article_count_row["n"] if article_count_row else 0
        avg_row_bytes = (articles_bytes / article_count) if article_count else 0.0
        daily_growth_bytes = avg_rows_per_day * avg_row_bytes
        if daily_growth_bytes > 0:
            remaining_bytes = max(ceiling_bytes - db_bytes, 0)
            projected_days_remaining = remaining_bytes / daily_growth_bytes

    return StorageUsage(
        database_size_bytes=db_bytes,
        ceiling_bytes=ceiling_bytes,
        pct_used=round((db_bytes / ceiling_bytes) * 100, 2) if ceiling_bytes else 0.0,
        table_sizes_bytes=sizes,
        daily_growth_bytes=daily_growth_bytes,
        projected_days_remaining=projected_days_remaining,
    )


def _resolve_cutoff_days(conn: DictConnection, setting_key: str, override_days: int | None) -> int:
    if override_days is not None:
        if override_days < 0:
            raise ValidationError(f"older_than_days must be >= 0, got {override_days}")
        return override_days
    return get_setting_int(conn, setting_key)


def purge_stale_bodies(
    conn: DictConnection,
    *,
    older_than_days: int | None = None,
    require_exported: bool = False,
    confirm: bool = False,
) -> PurgeResult:
    """Shed `article_bodies.content_text` (the single heaviest field —
    storage-policy.md §2) older than the body-retention window. Sets
    `articles.body_status = 'purged'` on the affected rows so callers
    can still tell "purged for space" apart from "never fetched"
    (frontend-contract.md §8 open question #10).
    """
    days = _resolve_cutoff_days(conn, "body_retention_days", older_than_days)
    where_export = "AND a.exported_at IS NOT NULL" if require_exported else ""

    count_row = conn.execute(
        f"""
        SELECT count(*) AS n
        FROM article_bodies b
        JOIN articles a ON a.id = b.article_id
        WHERE b.fetched_at < now() - (%s || ' days')::interval
        {where_export}
        """,
        (days,),
    ).fetchone()
    would_affect = count_row["n"] if count_row else 0

    if not confirm:
        return PurgeResult(
            "purge_stale_bodies", confirmed=False, rows_affected=would_affect, detail=f"older_than_days={days}"
        )

    with conn.transaction():
        deleted = conn.execute(
            f"""
            DELETE FROM article_bodies b
            USING articles a
            WHERE a.id = b.article_id
              AND b.fetched_at < now() - (%s || ' days')::interval
              {where_export}
            RETURNING b.article_id
            """,
            (days,),
        ).fetchall()
        ids = [row["article_id"] for row in deleted]
        if ids:
            conn.execute(
                "UPDATE articles SET body_status = 'purged' WHERE id = ANY(%s)",
                (ids,),
            )
    return PurgeResult(
        "purge_stale_bodies", confirmed=True, rows_affected=len(ids), detail=f"older_than_days={days}"
    )


def purge_stale_unmatched_articles(
    conn: DictConnection,
    *,
    older_than_days: int | None = None,
    confirm: bool = False,
) -> PurgeResult:
    """Compact `review_status='filtered_out'` rows older than the raw
    unmatched-retention window into `seen_urls` (storage-policy.md §2) —
    preserves cross-run dedup integrity forever while dropping the full
    row (title, url, etc.) for articles that never matched a keyword and
    were therefore never eligible for review or export in the first
    place. Never touches `review_status IN ('pending','approved',
    'rejected')` rows — those are the "matched, retained indefinitely"
    tier, purged only by `purge_matched_articles` below, manually.
    """
    days = _resolve_cutoff_days(conn, "raw_retention_days", older_than_days)

    count_row = conn.execute(
        """
        SELECT count(*) AS n FROM articles
        WHERE review_status = 'filtered_out' AND day < (CURRENT_DATE - %s::int)
        """,
        (days,),
    ).fetchone()
    would_affect = count_row["n"] if count_row else 0

    if not confirm:
        return PurgeResult(
            "purge_stale_unmatched_articles",
            confirmed=False,
            rows_affected=would_affect,
            detail=f"older_than_days={days}",
        )

    with conn.transaction():
        conn.execute(
            """
            INSERT INTO seen_urls (url_norm, first_seen_day)
            SELECT url_norm, day FROM articles
            WHERE review_status = 'filtered_out' AND day < (CURRENT_DATE - %s::int) AND url_norm <> ''
            ON CONFLICT (url_norm) DO NOTHING
            """,
            (days,),
        )
        deleted = conn.execute(
            """
            DELETE FROM articles
            WHERE review_status = 'filtered_out' AND day < (CURRENT_DATE - %s::int)
            RETURNING id
            """,
            (days,),
        ).fetchall()
    return PurgeResult(
        "purge_stale_unmatched_articles",
        confirmed=True,
        rows_affected=len(deleted),
        detail=f"older_than_days={days}",
    )


def purge_matched_articles(
    conn: DictConnection,
    *,
    older_than: date,
    require_exported: bool = True,
    confirm: bool = False,
) -> PurgeResult:
    """The manual "release valve" (ADR §9.2): admin-triggered deletion
    of reviewed/matched article rows older than an explicit cutoff date.
    Unlike the two hygiene purges above, this is NEVER called
    automatically/on a schedule — it is the operator's deliberate choice
    to reclaim space from the indefinitely-retained tier.

    `require_exported=True` (the default, and the recommended setting)
    enforces DoD #5's "purge must never delete anything not yet
    exported if the operator asked for export-first": only rows with
    `exported_at IS NOT NULL` are eligible. Pass `require_exported=False`
    only when the operator has explicitly decided the export step is
    unnecessary (e.g. re-purging rows already confirmed exported by a
    prior run, or deliberately discarding without a backup).
    """
    where_export = "AND exported_at IS NOT NULL" if require_exported else ""

    count_row = conn.execute(
        f"SELECT count(*) AS n FROM articles WHERE day < %s {where_export}",
        (older_than,),
    ).fetchone()
    would_affect = count_row["n"] if count_row else 0

    if not confirm:
        return PurgeResult(
            "purge_matched_articles",
            confirmed=False,
            rows_affected=would_affect,
            detail=f"older_than={older_than.isoformat()} require_exported={require_exported}",
        )

    with conn.transaction():
        deleted = conn.execute(
            f"DELETE FROM articles WHERE day < %s {where_export} RETURNING id",
            (older_than,),
        ).fetchall()
    return PurgeResult(
        "purge_matched_articles",
        confirmed=True,
        rows_affected=len(deleted),
        detail=f"older_than={older_than.isoformat()} require_exported={require_exported}",
    )
