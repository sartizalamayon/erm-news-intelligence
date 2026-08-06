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
§3 — they are safe to run routinely (e.g. daily, from the batch job:
call once with `confirm=False`, then immediately again with
`confirm=True` — no human required for either call, see "Closing RT-6"
below) since neither touches reviewed/exported content. The third purge
(matched/reviewed articles) is the deliberate "release valve" ADR §9.2
describes and is meant to be admin-invoked occasionally, not scheduled.

Closing RT-6 — preview and confirm are no longer two unlocked calls
-------------------------------------------------------------------
Previously, `confirm=False` and `confirm=True` ran the *identical*
time-relative `WHERE` clause as two independent, unsynchronized
queries. If any writer (the daily batch job, another admin, anything)
inserted or aged a new row into that same `WHERE` clause between the
two calls, `confirm=True` would silently delete more rows than the
`confirm=False` preview had shown — a real gap, found by RED-TEAM
(RT-6, docs/red-team-findings.md), between what an operator approves
and what actually gets destroyed.

Fixed by making `confirm=True` operate on an explicit ID *snapshot*
taken at preview time, not a live re-evaluation of the predicate:
`confirm=False` computes the exact qualifying row ids (not just a
count) and persists them; `confirm=True` looks up that persisted
snapshot for the identical (operation, parameters) pair, and deletes
`WHERE id = ANY(<those exact ids>)` — intersected with the original
safety predicate again as defense-in-depth, so the delete can only ever
be a *subset* of the snapshot, never a superset, and never something
that's independently stopped qualifying (e.g. already gone). A
`confirm=True` call with no matching preview snapshot on record raises
`ValidationError` rather than falling back to a live query — there is
no code path left that deletes based on a freshly-recomputed WHERE
clause.

Where the snapshot lives: reused from the existing `settings` key/value
table (`0001_settings.sql`, ARCH-DB-owned) rather than a new dedicated
table — as of this fix, migration numbers 0008+ are AUTH-OTP's
namespace (per the coordinator, to avoid numbering collisions between
concurrently-working agents), so adding a new migration isn't available
to this task. `settings` is structurally nothing but a TEXT key/value
store; using it for a short-lived purge snapshot rather than long-lived
admin config is a genuine stretch of its documented purpose, which is
why every snapshot key is namespaced under `purge_preview::` and is
always consumed (deleted) the instant it's read — successfully or not —
so it never lingers as visible clutter in an admin config listing, and
is also independently expired after `_PREVIEW_STALENESS` so a
forgotten preview from hours ago can't silently authorize a delete now.
A real Postgres server, not an in-process cache, is deliberate too: an
in-memory dict would not survive Render's free-tier 15-minute idle
sleep (ADR §1) between an operator previewing and later confirming.

**What this closes vs. narrows — stated plainly, not glossed over:**
the exact race RT-6 describes and tests (a background writer growing
the eligible set between one preview call and one confirm call) is
**fully closed** — `confirm=True` can never delete a row that was not
present in *some* real, previously-computed preview snapshot for that
exact operation and parameters. One adjacent scenario this does **not**
fully solve: if *two different* preview calls for the identical
operation+parameters race each other before either is confirmed, the
second preview's `set_setting` overwrites the first's snapshot (last
preview wins, since both share one key) — so a `confirm=True` might act
on a different, but still real and previously-shown, snapshot than the
one a specific operator personally looked at. That does not reopen
RT-6's failure mode (every id ever deleted was shown in some preview,
never invented at confirm time), but it is a narrower, honestly-flagged
residual gap for the rare case of two concurrent previews of the same
operation, not claimed to be solved here.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from app.db.connection import DictConnection
from app.db.queries import database_size_bytes, recent_daily_row_counts, table_sizes_bytes
from app.db.settings import get_setting_int, set_setting
from app.errors import ValidationError

MiB = 1024 * 1024

# How long a dry-run preview snapshot remains confirmable. Chosen to
# comfortably cover a human looking at an admin screen and clicking
# confirm, while refusing to honor a preview so old it's more likely
# forgotten than intentionally delayed.
_PREVIEW_STALENESS = timedelta(hours=1)


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
    # the exact ids previewed (confirmed=False) or actually deleted (confirmed=True) -- see "Closing RT-6" above
    row_ids: list[int] = field(default_factory=list)


def _preview_snapshot_key(operation: str, params_key: str) -> str:
    return f"purge_preview::{operation}::{params_key}"


def _save_preview_snapshot(conn: DictConnection, operation: str, params_key: str, row_ids: list[int]) -> None:
    """Persist the exact row-id set a `confirm=False` preview computed,
    keyed by (operation, parameters) — see "Closing RT-6" above.
    """
    payload = json.dumps({"row_ids": row_ids, "created_at": datetime.now(timezone.utc).isoformat()})
    set_setting(conn, _preview_snapshot_key(operation, params_key), payload)


def _consume_preview_snapshot(conn: DictConnection, operation: str, params_key: str) -> list[int] | None:
    """Read and delete the snapshot saved by `_save_preview_snapshot` for
    this exact (operation, parameters) pair. Returns `None` (callers
    treat this as "no preview on record — confirm refused") if no
    snapshot exists, if it's malformed, or if it's older than
    `_PREVIEW_STALENESS`. Always deletes whatever row it finds,
    regardless of outcome, so a snapshot can be consumed at most once —
    replaying the same confirm call twice fails the second time rather
    than deleting twice.
    """
    key = _preview_snapshot_key(operation, params_key)
    row = conn.execute("SELECT value FROM settings WHERE key = %s", (key,)).fetchone()
    if row is None or not row["value"]:
        return None
    conn.execute("DELETE FROM settings WHERE key = %s", (key,))
    try:
        data = json.loads(row["value"])
        created_at = datetime.fromisoformat(data["created_at"])
        row_ids = [int(i) for i in data["row_ids"]]
    except (ValueError, KeyError, TypeError):
        return None
    if datetime.now(timezone.utc) - created_at > _PREVIEW_STALENESS:
        return None
    return row_ids


def _require_snapshot(conn: DictConnection, operation: str, params_key: str, detail: str) -> list[int]:
    snapshot_ids = _consume_preview_snapshot(conn, operation, params_key)
    if snapshot_ids is None:
        raise ValidationError(
            f"no matching dry-run preview found for {operation}({detail}) — call with confirm=False "
            "first so the exact row set is shown before anything is deleted (RT-6)"
        )
    return snapshot_ids


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

    `confirm=False` snapshots the exact qualifying `article_id`s;
    `confirm=True` requires that snapshot (from a matching prior preview
    call) and deletes only those ids, re-checked against the live
    predicate — see the module docstring's "Closing RT-6".
    """
    days = _resolve_cutoff_days(conn, "body_retention_days", older_than_days)
    where_export = "AND a.exported_at IS NOT NULL" if require_exported else ""
    detail = f"older_than_days={days} require_exported={require_exported}"
    params_key = detail

    if not confirm:
        rows = conn.execute(
            f"""
            SELECT b.article_id AS id
            FROM article_bodies b
            JOIN articles a ON a.id = b.article_id
            WHERE b.fetched_at < now() - (%s || ' days')::interval
            {where_export}
            ORDER BY b.article_id
            """,
            (days,),
        ).fetchall()
        ids = [row["id"] for row in rows]
        _save_preview_snapshot(conn, "purge_stale_bodies", params_key, ids)
        return PurgeResult("purge_stale_bodies", confirmed=False, rows_affected=len(ids), detail=detail, row_ids=ids)

    snapshot_ids = _require_snapshot(conn, "purge_stale_bodies", params_key, detail)

    with conn.transaction():
        deleted = conn.execute(
            f"""
            DELETE FROM article_bodies b
            USING articles a
            WHERE a.id = b.article_id
              AND b.article_id = ANY(%(ids)s)
              AND b.fetched_at < now() - (%(days)s || ' days')::interval
              {where_export}
            RETURNING b.article_id
            """,
            {"ids": snapshot_ids, "days": days},
        ).fetchall()
        ids = [row["article_id"] for row in deleted]
        if ids:
            conn.execute(
                "UPDATE articles SET body_status = 'purged' WHERE id = ANY(%s)",
                (ids,),
            )
    return PurgeResult("purge_stale_bodies", confirmed=True, rows_affected=len(ids), detail=detail, row_ids=ids)


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

    `confirm=False` snapshots the exact qualifying `articles.id`s;
    `confirm=True` requires that snapshot (from a matching prior preview
    call) and deletes only those ids, re-checked against the live
    predicate — see the module docstring's "Closing RT-6".
    """
    days = _resolve_cutoff_days(conn, "raw_retention_days", older_than_days)
    detail = f"older_than_days={days}"
    params_key = detail

    if not confirm:
        rows = conn.execute(
            """
            SELECT id FROM articles
            WHERE review_status = 'filtered_out' AND day < (CURRENT_DATE - %s::int)
            ORDER BY id
            """,
            (days,),
        ).fetchall()
        ids = [row["id"] for row in rows]
        _save_preview_snapshot(conn, "purge_stale_unmatched_articles", params_key, ids)
        return PurgeResult(
            "purge_stale_unmatched_articles", confirmed=False, rows_affected=len(ids), detail=detail, row_ids=ids
        )

    snapshot_ids = _require_snapshot(conn, "purge_stale_unmatched_articles", params_key, detail)

    with conn.transaction():
        conn.execute(
            """
            INSERT INTO seen_urls (url_norm, first_seen_day)
            SELECT url_norm, day FROM articles
            WHERE id = ANY(%(ids)s)
              AND review_status = 'filtered_out' AND day < (CURRENT_DATE - %(days)s::int) AND url_norm <> ''
            ON CONFLICT (url_norm) DO NOTHING
            """,
            {"ids": snapshot_ids, "days": days},
        )
        deleted = conn.execute(
            """
            DELETE FROM articles
            WHERE id = ANY(%(ids)s)
              AND review_status = 'filtered_out' AND day < (CURRENT_DATE - %(days)s::int)
            RETURNING id
            """,
            {"ids": snapshot_ids, "days": days},
        ).fetchall()
    deleted_ids = [row["id"] for row in deleted]
    return PurgeResult(
        "purge_stale_unmatched_articles",
        confirmed=True,
        rows_affected=len(deleted_ids),
        detail=detail,
        row_ids=deleted_ids,
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

    `confirm=False` snapshots the exact qualifying `articles.id`s;
    `confirm=True` requires that snapshot (from a matching prior preview
    call) and deletes only those ids, re-checked against the live
    predicate — see the module docstring's "Closing RT-6". This is the
    purge most likely to run right after an export (ADR §9.2's ruled
    *export → purge → re-import* journey), so it is exactly the one
    where a diverging preview/delete count would be most visible and
    most likely to erode operator trust.
    """
    where_export = "AND exported_at IS NOT NULL" if require_exported else ""
    detail = f"older_than={older_than.isoformat()} require_exported={require_exported}"
    params_key = detail

    if not confirm:
        rows = conn.execute(
            f"SELECT id FROM articles WHERE day < %s {where_export} ORDER BY id",
            (older_than,),
        ).fetchall()
        ids = [row["id"] for row in rows]
        _save_preview_snapshot(conn, "purge_matched_articles", params_key, ids)
        return PurgeResult(
            "purge_matched_articles", confirmed=False, rows_affected=len(ids), detail=detail, row_ids=ids
        )

    snapshot_ids = _require_snapshot(conn, "purge_matched_articles", params_key, detail)

    with conn.transaction():
        deleted = conn.execute(
            f"DELETE FROM articles WHERE id = ANY(%(ids)s) AND day < %(cutoff)s {where_export} RETURNING id",
            {"ids": snapshot_ids, "cutoff": older_than},
        ).fetchall()
    deleted_ids = [row["id"] for row in deleted]
    return PurgeResult(
        "purge_matched_articles", confirmed=True, rows_affected=len(deleted_ids), detail=detail, row_ids=deleted_ids
    )
