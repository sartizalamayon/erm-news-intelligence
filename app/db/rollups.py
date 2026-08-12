"""Rollup population -- computes migrations/0007_rollups.sql's
`rollup_monthly_section` / `_source` / `_score_band` / `_theme` tables
from the real `articles` table, so `app.api.routers.dashboard`'s
`/dashboard/trends` (the long-term dashboard tab) has real data to read
instead of empty tables. `macro_indicator_values` is explicitly out of
scope here -- 0007's own header comment notes it has "no relationship to
`articles` at all" (external economic data, admin-entered/ingested
separately); this module only derives rollups from the collected corpus.

Grain and idempotency: the rollup tables are MONTHLY (0007's header:
"grain is monthly ... because frontend-contract.md needs 12 *monthly*
points"). `compute_rollups_for_month`/`compute_rollups_for_day` therefore
each recompute an entire calendar month FROM SCRATCH via a fresh
aggregate query over `articles`, then UPSERT the result as an ABSOLUTE
`SET` -- `ON CONFLICT (...) DO UPDATE SET x = EXCLUDED.x` -- never as a
`x = table.x + EXCLUDED.x` delta increment.

This deliberately does NOT reuse `app.db.queries.upsert_rollup_monthly_*`
(those four helpers already exist, but are additive/delta-based --
`article_count = rollup_monthly_theme.article_count + EXCLUDED.article_count`
-- which is exactly wrong for "call this again for the same day": a
second call would double-count. This module writes its own SQL directly
against the rollup tables instead, following the SAME idempotent-write
shape `app/db/settings.py::set_setting` already uses elsewhere in this
codebase (`ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value` --
absolute overwrite, not accumulation). Running this module's functions
twice for the same day (or twice for the whole corpus, via backfill)
produces byte-identical rollup rows both times -- proven in
tests/test_rollups.py.

Definitions (mirroring the exact patterns `app.api.routers.dashboard`'s
`dashboard_today` already established, so short-term and long-term
numbers agree on what "matched" means):

- section fetched_count / matched_count: one row per (article, section)
  pair via `unnest(string_to_array(sections, ', '))`, same as
  `dashboard_today`'s `by_section` query. `matched` = `review_status !=
  'filtered_out'`, same boundary `dashboard_today` uses for its
  `matched_by_day` series.
- source fetched_count / matched_count: same `matched` boundary, grouped
  by `source` instead of section.
- score_band article_count: the same 4-tier `llm_score` boundaries
  `dashboard_today`'s `rel_mix` query uses (>=8 high / [6,8) medium /
  [5,6) low / <5 least), restricted to `llm_score IS NOT NULL` (only
  scored articles have a band).
- theme article_count: grouped by `topic_tag`, `topic_tag <> ''` (same
  predicate `idx_articles_topic_tag` is a partial index for).
- theme intensity: normalized against that THEME'S OWN max article_count
  across its full history (not a global cross-theme max) -- 0007's own
  header comment flags this as an open normalization choice ("whichever
  normalization the dashboard agent picks, this column is where it
  lands"; frontend-contract.md §8 open question 7 leaves it unresolved
  too). Recomputed after every write for every touched theme across ALL
  its months (not just the month just written), so a later month that
  raises a theme's all-time max correctly rescales its earlier months
  too -- this is what makes intensity values stable and idempotent
  across an out-of-order backfill.

Stale-row correctness: a recompute also zeroes out any existing row for
that month whose key (section/source/theme) is no longer present in the
fresh aggregate -- e.g. if every article that gave a source a nonzero
count in some month is later purged (storage-policy.md §2's raw-retention
purges). Score-band rows are always written for all four bands (the
GROUP BY-free aggregate always returns a value, 0 or not), so no
zero-out pass is needed there.
"""

from __future__ import annotations

import sys
from datetime import date

from app.db.connection import DictConnection, get_connection
from app.logging import configure_logging, get_logger

logger = get_logger(__name__)

_SCORE_BANDS = ("high", "medium", "low", "least")


def _month_start(d: date) -> date:
    return d.replace(day=1)


def _next_month(d: date) -> date:
    return date(d.year + (d.month == 12), (d.month % 12) + 1, 1)


def _zero_out_missing(
    conn: DictConnection,
    *,
    table: str,
    key_col: str,
    month: date,
    present_keys: set[str],
    count_cols: tuple[str, ...],
) -> None:
    """Set every count column to 0 for rows of `table` at `month` whose
    key is not in `present_keys` -- the "this key had data before, now it
    doesn't" correction described in the module docstring. Table/column
    names here are always one of this module's own hardcoded constants,
    never caller/user input, so building the SET clause with an f-string
    is safe (no untrusted value ever reaches the SQL text itself; all
    real values go through parameterized placeholders below).
    """
    set_clause = ", ".join(f"{c} = 0" for c in count_cols)
    if present_keys:
        conn.execute(
            f"UPDATE {table} SET {set_clause} WHERE month = %s AND {key_col} != ALL(%s)",  # noqa: S608
            (month, list(present_keys)),
        )
    else:
        conn.execute(
            f"UPDATE {table} SET {set_clause} WHERE month = %s",  # noqa: S608
            (month,),
        )


def _upsert_sections(conn: DictConnection, month: date, month_end: date) -> int:
    rows = conn.execute(
        """
        SELECT sec AS section,
               count(*) AS fetched_count,
               count(*) FILTER (WHERE a.review_status != 'filtered_out') AS matched_count
        FROM articles a, unnest(string_to_array(a.sections, ', ')) AS sec
        WHERE a.day >= %s AND a.day < %s AND a.sections <> ''
        GROUP BY sec
        """,
        (month, month_end),
    ).fetchall()
    present = {r["section"] for r in rows}
    _zero_out_missing(
        conn, table="rollup_monthly_section", key_col="section", month=month, present_keys=present,
        count_cols=("fetched_count", "matched_count"),
    )
    for r in rows:
        conn.execute(
            """
            INSERT INTO rollup_monthly_section (month, section, fetched_count, matched_count)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (month, section) DO UPDATE SET
                fetched_count = EXCLUDED.fetched_count,
                matched_count = EXCLUDED.matched_count
            """,
            (month, r["section"], int(r["fetched_count"]), int(r["matched_count"])),
        )
    return len(rows)


def _upsert_sources(conn: DictConnection, month: date, month_end: date) -> int:
    rows = conn.execute(
        """
        SELECT source,
               count(*) AS fetched_count,
               count(*) FILTER (WHERE review_status != 'filtered_out') AS matched_count
        FROM articles
        WHERE day >= %s AND day < %s
        GROUP BY source
        """,
        (month, month_end),
    ).fetchall()
    present = {r["source"] for r in rows}
    _zero_out_missing(
        conn, table="rollup_monthly_source", key_col="source", month=month, present_keys=present,
        count_cols=("fetched_count", "matched_count"),
    )
    for r in rows:
        conn.execute(
            """
            INSERT INTO rollup_monthly_source (month, source, fetched_count, matched_count)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (month, source) DO UPDATE SET
                fetched_count = EXCLUDED.fetched_count,
                matched_count = EXCLUDED.matched_count
            """,
            (month, r["source"], int(r["fetched_count"]), int(r["matched_count"])),
        )
    return len(rows)


def _upsert_score_bands(conn: DictConnection, month: date, month_end: date) -> int:
    row = conn.execute(
        """
        SELECT
          count(*) FILTER (WHERE llm_score >= 8) AS high,
          count(*) FILTER (WHERE llm_score >= 6 AND llm_score < 8) AS medium,
          count(*) FILTER (WHERE llm_score >= 5 AND llm_score < 6) AS low,
          count(*) FILTER (WHERE llm_score < 5) AS least
        FROM articles
        WHERE day >= %s AND day < %s AND llm_score IS NOT NULL
        """,
        (month, month_end),
    ).fetchone()
    row = row or {}
    for band in _SCORE_BANDS:
        conn.execute(
            """
            INSERT INTO rollup_monthly_score_band (month, band, article_count)
            VALUES (%s, %s, %s)
            ON CONFLICT (month, band) DO UPDATE SET
                article_count = EXCLUDED.article_count
            """,
            (month, band, int(row.get(band) or 0)),
        )
    return len(_SCORE_BANDS)


def _upsert_themes(conn: DictConnection, month: date, month_end: date) -> int:
    rows = conn.execute(
        """
        SELECT topic_tag AS theme, count(*) AS article_count
        FROM articles
        WHERE day >= %s AND day < %s AND topic_tag <> ''
        GROUP BY topic_tag
        """,
        (month, month_end),
    ).fetchall()
    present = {r["theme"] for r in rows}
    _zero_out_missing(
        conn, table="rollup_monthly_theme", key_col="theme", month=month, present_keys=present,
        count_cols=("article_count",),
    )
    for r in rows:
        conn.execute(
            """
            INSERT INTO rollup_monthly_theme (month, theme, article_count, intensity)
            VALUES (%s, %s, %s, 0)
            ON CONFLICT (month, theme) DO UPDATE SET
                article_count = EXCLUDED.article_count
            """,
            (month, r["theme"], int(r["article_count"])),
        )

    # Themes touched this month (fresh nonzero rows) plus any theme that
    # already had a row for this month (including one just zeroed out) --
    # both need their intensity rescaled against their (possibly changed)
    # all-time max. See module docstring's "theme intensity" note.
    existing_rows = conn.execute(
        "SELECT DISTINCT theme FROM rollup_monthly_theme WHERE month = %s", (month,)
    ).fetchall()
    touched = present | {r["theme"] for r in existing_rows}
    if touched:
        conn.execute(
            """
            UPDATE rollup_monthly_theme t
            SET intensity = CASE WHEN mx.max_count > 0 THEN t.article_count::numeric / mx.max_count ELSE 0 END
            FROM (
                SELECT theme, max(article_count) AS max_count
                FROM rollup_monthly_theme
                WHERE theme = ANY(%s)
                GROUP BY theme
            ) mx
            WHERE t.theme = mx.theme
            """,
            (list(touched),),
        )
    return len(rows)


def compute_rollups_for_month(conn: DictConnection, month: date) -> dict[str, int]:
    """Recompute every rollup_monthly_* row for the calendar month
    containing `month` (any day within the month works; it is normalized
    to the 1st) from the real `articles` table, and UPSERT the result as
    an absolute replace. Idempotent by construction -- see module
    docstring. Returns row counts written, for logging/tests.
    """
    month = _month_start(month)
    month_end = _next_month(month)

    counts = {
        "sections": _upsert_sections(conn, month, month_end),
        "sources": _upsert_sources(conn, month, month_end),
        "score_bands": _upsert_score_bands(conn, month, month_end),
        "themes": _upsert_themes(conn, month, month_end),
    }
    logger.info("rollups_computed", extra={"month": month.isoformat(), **counts})
    return counts


def compute_rollups_for_day(conn: DictConnection, day: date) -> dict[str, int]:
    """Convenience wrapper -- recomputes the calendar month containing
    `day`. This is the entrypoint `app.batch.run_daily.main()` calls once
    per real batch run, and what `tests/test_rollups.py` calls directly
    to prove same-day idempotency.
    """
    return compute_rollups_for_month(conn, day)


def backfill_all_days(conn: DictConnection) -> dict[str, int]:
    """One-off backfill: recompute rollups for every historical day that
    has data in `articles`. Iterates one call per DISTINCT `day` present
    (not one call per distinct month) so this literally covers "every
    historical day that has data" per-call, even though each call
    internally recomputes its whole containing month -- some months get
    recomputed more than once, which is harmless (idempotent, see module
    docstring) and cheap at this data's real scale (a few thousand rows,
    dozens of distinct days -- see storage-policy.md §6).
    """
    rows = conn.execute("SELECT DISTINCT day FROM articles WHERE day IS NOT NULL ORDER BY day").fetchall()
    days = [r["day"] for r in rows]
    months_seen: set[date] = set()
    for d in days:
        compute_rollups_for_day(conn, d)
        months_seen.add(_month_start(d))
    conn.commit()
    result = {"days_processed": len(days), "distinct_months": len(months_seen)}
    logger.info("rollups_backfill_complete", extra=result)
    return result


def _cli_backfill() -> int:
    configure_logging()
    with get_connection() as conn:
        result = backfill_all_days(conn)
    logger.info("rollups_backfill_cli_done", extra=result)
    print(result)  # noqa: T201 -- CLI entrypoint, this is the intended user-visible output
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] != "backfill":
        print("usage: python -m app.db.rollups backfill")  # noqa: T201
        raise SystemExit(2)
    raise SystemExit(_cli_backfill())
