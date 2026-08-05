"""A small set of reusable, parametrized queries — not a full CRUD/ORM
layer (GATE 0: "no ORM ceremony beyond what's needed"; building out
every screen's query surface is ADMIN-API/FE-WIRE's job, not ARCH-DB's).

What lives here is query logic that is either (a) load-bearing for a
design decision made elsewhere in this package and worth having one
tested, canonical implementation of — the dedup-insert fix
(migrations/0003_articles.sql) and the storage introspection queries
(app/services/storage.py) — or (b) simple enough and central enough
(the rollup upserts) that duplicating it per-caller would be the wrong
kind of DRY violation.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from app.db.connection import DictConnection


def insert_article(conn: DictConnection, **fields: Any) -> int | None:
    """Insert one article with the dedup-on-insert fix
    (migrations/0003_articles.sql): `ON CONFLICT (url_norm) DO NOTHING`
    replaces legacy's `SELECT 1 FROM articles WHERE url=? OR url_norm=?`
    full-table scan (ADR §2 point 1, §6) with a single index lookup done
    by the INSERT itself.

    Returns the new row's id, or None if it was a duplicate (already
    present by url_norm) and nothing was inserted — mirrors legacy's
    `dropped_dupe` counter semantics for the caller to tally.

    `fields` must include at minimum: url, url_norm, source, title, day.
    Everything else falls back to its column default. This function
    does NOT check `seen_urls` (migrations/0004) — that fallback check
    for URLs whose full row has already aged out of the raw-retention
    window is the pipeline write-path's responsibility (P1-FETCH), since
    it needs to happen *before* this INSERT is attempted, not inside it.
    """
    columns = list(fields.keys())
    placeholders = ", ".join(f"%({c})s" for c in columns)
    column_list = ", ".join(columns)
    row = conn.execute(
        f"""
        INSERT INTO articles ({column_list})
        VALUES ({placeholders})
        ON CONFLICT (url_norm) WHERE url_norm <> '' DO NOTHING
        RETURNING id
        """,
        fields,
    ).fetchone()
    return row["id"] if row else None


def url_norm_seen(conn: DictConnection, url_norm: str) -> bool:
    """The `seen_urls` half of the two-step dedup check
    (migrations/0004_article_bodies_and_seen_urls.sql) — for a URL whose
    `articles` row may have already been purged past the raw-retention
    window. Still an O(log n) primary-key lookup, never a scan.
    """
    row = conn.execute("SELECT 1 FROM seen_urls WHERE url_norm = %s", (url_norm,)).fetchone()
    return row is not None


def remember_seen_url(conn: DictConnection, url_norm: str, first_seen_day: date) -> None:
    conn.execute(
        "INSERT INTO seen_urls (url_norm, first_seen_day) VALUES (%s, %s) ON CONFLICT (url_norm) DO NOTHING",
        (url_norm, first_seen_day),
    )


# ── Storage introspection (backs app/services/storage.py, DoD #4) ──────


def database_size_bytes(conn: DictConnection) -> int:
    row = conn.execute("SELECT pg_database_size(current_database()) AS bytes").fetchone()
    if row is None:
        return 0
    return int(row["bytes"])


def table_sizes_bytes(conn: DictConnection, schema: str | None = None) -> dict[str, int]:
    """Per-table size in bytes via `pg_total_relation_size` — includes
    the table's own heap, its indexes, and TOAST storage, i.e. the full
    cost of that table, not just row data. This is real Postgres
    introspection (DoD #4), not an estimate derived from row counts.

    `schema=None` (the default) resolves to `current_schema()` at query
    time rather than hardcoding `'public'` — correct for Supabase's
    default `public` schema in production *and* for a test running
    inside an isolated per-test schema (app/db/testing.py) without the
    caller needing to know which.
    """
    rows = conn.execute(
        """
        SELECT c.relname AS table_name, pg_total_relation_size(c.oid) AS bytes
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = COALESCE(%s, current_schema()) AND c.relkind = 'r'
        ORDER BY bytes DESC
        """,
        (schema,),
    ).fetchall()
    return {row["table_name"]: int(row["bytes"]) for row in rows}


def recent_daily_row_counts(conn: DictConnection, days: int) -> dict[date, int]:
    """`articles` rows inserted per day over the last `days` days — the
    real, measured growth-rate input to the days-remaining projection in
    app/services/storage.py, rather than a hardcoded constant.
    """
    rows = conn.execute(
        """
        SELECT day, count(*) AS n
        FROM articles
        WHERE day >= (CURRENT_DATE - %s::int)
        GROUP BY day
        ORDER BY day
        """,
        (days,),
    ).fetchall()
    return {row["day"]: int(row["n"]) for row in rows}


# ── Rollup upserts (migrations/0007_rollups.sql) ────────────────────────
#
# Population/scheduling is the dashboard/analytics owner's call (see
# 0007's header comment) — these are the tested, canonical write
# primitives so that whoever wires the nightly rollup job isn't
# reinventing the upsert shape (or getting the ON CONFLICT arithmetic
# wrong) per call site.


def upsert_rollup_monthly_section(
    conn: DictConnection, month: date, section: str, *, fetched_delta: int = 0, matched_delta: int = 0
) -> None:
    conn.execute(
        """
        INSERT INTO rollup_monthly_section (month, section, fetched_count, matched_count)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (month, section) DO UPDATE SET
            fetched_count = rollup_monthly_section.fetched_count + EXCLUDED.fetched_count,
            matched_count = rollup_monthly_section.matched_count + EXCLUDED.matched_count
        """,
        (month, section, fetched_delta, matched_delta),
    )


def upsert_rollup_monthly_source(
    conn: DictConnection, month: date, source: str, *, fetched_delta: int = 0, matched_delta: int = 0
) -> None:
    conn.execute(
        """
        INSERT INTO rollup_monthly_source (month, source, fetched_count, matched_count)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (month, source) DO UPDATE SET
            fetched_count = rollup_monthly_source.fetched_count + EXCLUDED.fetched_count,
            matched_count = rollup_monthly_source.matched_count + EXCLUDED.matched_count
        """,
        (month, source, fetched_delta, matched_delta),
    )


def upsert_rollup_monthly_score_band(conn: DictConnection, month: date, band: str, *, count_delta: int = 1) -> None:
    conn.execute(
        """
        INSERT INTO rollup_monthly_score_band (month, band, article_count)
        VALUES (%s, %s, %s)
        ON CONFLICT (month, band) DO UPDATE SET
            article_count = rollup_monthly_score_band.article_count + EXCLUDED.article_count
        """,
        (month, band, count_delta),
    )


def upsert_rollup_monthly_theme(
    conn: DictConnection, month: date, theme: str, *, count_delta: int = 1, intensity: float | None = None
) -> None:
    conn.execute(
        """
        INSERT INTO rollup_monthly_theme (month, theme, article_count, intensity)
        VALUES (%s, %s, %s, COALESCE(%s, 0))
        ON CONFLICT (month, theme) DO UPDATE SET
            article_count = rollup_monthly_theme.article_count + EXCLUDED.article_count,
            intensity = COALESCE(%s, rollup_monthly_theme.intensity)
        """,
        (month, theme, count_delta, intensity, intensity),
    )


def standing_themes(conn: DictConnection, since_month: date, min_consecutive_months: int = 9) -> list[dict[str, Any]]:
    """Themes covered in at least `min_consecutive_months` of the months
    since `since_month` — the query behind frontend-contract.md §3.2's
    `standing[]` (badge threshold `months >= 9` observed in the mock).
    Reads only `rollup_monthly_theme` (bounded, small), never raw
    `articles` — this is the whole point of the rollup design.
    """
    rows = conn.execute(
        """
        SELECT theme,
               sum(article_count) AS total_count,
               count(*) FILTER (WHERE article_count > 0) AS months_active
        FROM rollup_monthly_theme
        WHERE month >= %s
        GROUP BY theme
        HAVING count(*) FILTER (WHERE article_count > 0) >= %s
        ORDER BY total_count DESC
        """,
        (since_month, min_consecutive_months),
    ).fetchall()
    return [dict(row) for row in rows]


def touch_run_heartbeat(conn: DictConnection, run_id: int, at: datetime | None = None) -> None:
    """Update a run's heartbeat (migrations/0002_reference_tables.sql) —
    the mechanism a batch-job watchdog needs to tell "still running" from
    "the process died mid-run" apart, which legacy's `runs` table cannot
    do at all (16 of 110 rows permanently stuck at status='running',
    legacy-behavior.md §3). Writing the heartbeat itself is the batch
    job's job; this is just the query.
    """
    conn.execute(
        "UPDATE runs SET heartbeat_at = COALESCE(%s, now()) WHERE id = %s",
        (at, run_id),
    )
