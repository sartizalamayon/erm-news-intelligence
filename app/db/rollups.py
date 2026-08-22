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
- watch article_count (migrations/0021): headlines matching one of
  `app.db.watchlist`'s curated entity patterns. Deliberately NOT derived
  from `articles.keywords`, whose loose substring matching is tuned for
  recall into the review queue and gets a competitor's monthly count
  wrong by two orders of magnitude -- see that module's docstring for
  the measured evidence. `high_count` reuses the same `llm_score >= 8`
  boundary as the score bands so "high" means one thing on this screen.
- econ article_count (migrations/0025): the macro counterpart of the
  watch rows -- headlines matching one of `app.db.econaxes`' six
  curated economy axes (energy, trade/RMG, FX, banking, revenue/tax,
  capital markets). Same counting rule, same `llm_score >= 8` critical
  boundary and the same column shape as the watch rows minus
  `group_key`, because the two boards are read side by side and a
  telecom line has to be directly comparable to an economy line. Also
  deliberately NOT derived from `articles.keywords` or `topic_tag`, for
  the reason 0021's header gives: you cannot trend a theme whose name
  changes between months.
- coverage collected_count / collected_days (migrations/0021): whether
  a month was actually being monitored, as opposed to merely appearing
  in the corpus because a later crawl found backdated articles.
  `articles.day` is a publication date, so without this the long-term
  chart cannot tell "a quiet month" from "a month before the collector
  existed", and draws both as an empty bar.
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
from app.db.econaxes import (
    all_axes,
    axis_where_sql,
    econ_prefilter_pattern,
    normalized_text_sql,
)
from app.db.watchlist import (
    NORMALIZED_TEXT_SQL,
    all_entities,
    entity_predicate_sql,
    prefilter_pattern,
)
from app.logging import configure_logging, get_logger

logger = get_logger(__name__)

_SCORE_BANDS = ("high", "medium", "low", "least")

# `articles.day` is a publication DATE and `fetched_at` a timestamptz, so
# "was this article collected in the same calendar month it was
# published" needs one agreed timezone or the answer drifts with the
# server's TZ setting. Dhaka, matching the dashboard's own `_today_dhaka`
# and the app's single operating timezone.
DHAKA_TZ = "Asia/Dhaka"


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


def _upsert_watch(conn: DictConnection, month: date, month_end: date) -> int:
    """One row per (month, watchlist entity) with volume AND severity.

    A single pass over the month's articles computes every entity at
    once via `count(*) FILTER (WHERE <entity predicate>)`, rather than
    one query per entity: twelve entities x a dozen months of backfill
    would otherwise be ~150 separate scans for numbers that come out of
    one.

    The f-string builds SQL from `app.db.watchlist`'s frozen dataclasses
    only -- module constants, never a request parameter, a database
    value, or an admin-editable field -- exactly like `_zero_out_missing`
    above builds its SET clause from this module's own constants. Adding
    a watchlist entity is a code change, which is the point (see that
    module's docstring on why the admin-editable `keywords` table is the
    wrong source for a dashboard number).
    """
    entities = all_entities()
    if not entities:  # pragma: no cover -- the watchlist is never empty in practice
        return 0

    # The CTEs are MATERIALIZED on purpose. Written as plain nested
    # subqueries Postgres flattens them, which re-derives the
    # lower()/replace()/concat text and re-runs each entity regex inside
    # every FILTER clause: the month holding 45k real articles took ~13s
    # that way. `hits` applies one combined regex first
    # (watchlist.prefilter_pattern) so the twelve per-entity predicates
    # only ever run against the few hundred headlines that mention
    # telecom at all.
    flags = ", ".join(
        f"{entity_predicate_sql(entity, text_sql='hits.s')} AS e{index}"
        for index, (_group, entity) in enumerate(entities)
    )
    selects = ", ".join(
        f"count(*) FILTER (WHERE y.e{index}) AS n{index}, "
        f"count(*) FILTER (WHERE y.e{index} AND y.matched) AS m{index}, "
        f"count(*) FILTER (WHERE y.e{index} AND y.llm_score IS NOT NULL) AS s{index}, "
        f"count(*) FILTER (WHERE y.e{index} AND y.llm_score >= 8) AS h{index}, "
        f"coalesce(sum(y.llm_score) FILTER (WHERE y.e{index}), 0) AS t{index}"
        for index in range(len(entities))
    )
    row = conn.execute(
        f"""
        WITH base AS MATERIALIZED (
            SELECT (a.review_status != 'filtered_out') AS matched,
                   a.llm_score,
                   {NORMALIZED_TEXT_SQL} AS s
            FROM articles a
            WHERE a.day >= %s AND a.day < %s
        ),
        hits AS MATERIALIZED (
            SELECT base.matched, base.llm_score, base.s FROM base WHERE base.s ~ '{prefilter_pattern()}'
        ),
        y AS MATERIALIZED (
            SELECT hits.matched, hits.llm_score, {flags} FROM hits
        )
        SELECT {selects} FROM y
        """,  # noqa: S608
        (month, month_end),
    ).fetchone()
    row = row or {}

    for index, (group, entity) in enumerate(entities):
        conn.execute(
            """
            INSERT INTO rollup_monthly_watch
                (month, group_key, entity, article_count, matched_count, scored_count, high_count, score_sum)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (month, group_key, entity) DO UPDATE SET
                article_count = EXCLUDED.article_count,
                matched_count = EXCLUDED.matched_count,
                scored_count  = EXCLUDED.scored_count,
                high_count    = EXCLUDED.high_count,
                score_sum     = EXCLUDED.score_sum
            """,
            (
                month,
                group.key,
                entity.key,
                int(row.get(f"n{index}") or 0),
                int(row.get(f"m{index}") or 0),
                int(row.get(f"s{index}") or 0),
                int(row.get(f"h{index}") or 0),
                float(row.get(f"t{index}") or 0),
            ),
        )
    # Every entity is written every time (the aggregate always returns a
    # value, zero or not), so there is no stale-key zero-out pass to do
    # here -- same reasoning as the score bands above. An entity REMOVED
    # from the watchlist does leave rows behind; the read side selects by
    # the current watchlist, so those rows are inert rather than wrong.
    return len(entities)


def _upsert_econ(conn: DictConnection, month: date, month_end: date) -> int:
    """One row per (month, economy axis) with volume AND severity --
    migrations/0025's `rollup_monthly_econ`, the macro counterpart of
    `_upsert_watch` above.

    Same single-pass shape and for the same reason: six axes x a dozen
    months of backfill would otherwise be ~70 separate regex scans for
    numbers that come out of one. Same MATERIALIZED CTEs, so Postgres
    cannot flatten them and re-derive the normalized headline text
    inside every FILTER clause, and same combined-prefilter first pass
    (`econaxes.econ_prefilter_pattern`) so the per-axis predicates only
    ever run against headlines that mention the economy at all.

    That prefilter runs against `normalized_text_sql`, NOT the stripped
    `econ_text_sql`, and the difference is worth 2x on a real month.
    `ECON_STRIP_RE` only ever removes characters, so it can never turn a
    non-match into a match and filtering on the unstripped text stays a
    strict superset -- which keeps a per-row `regexp_replace` off all
    45,000 articles and pays for it only on the few hundred survivors.

    The one structural difference from `_upsert_watch` is parameter
    handling. `watchlist.entity_predicate_sql` weaves its patterns into
    the SQL text; `econaxes.axis_where_sql` returns `%s` placeholders
    plus the patterns as bind parameters, so this function has to
    concatenate the per-axis param lists in the SAME order it
    concatenates the per-axis SQL fragments. `flag_params` below is
    built in the loop that builds `flags` precisely to keep those two
    orders impossible to desynchronize.
    """
    axes = all_axes()
    if not axes:  # pragma: no cover -- the axis list is never empty in practice
        return 0

    flags: list[str] = []
    flag_params: list[str] = []
    for index, axis in enumerate(axes):
        clause, params = axis_where_sql(axis, table_alias="hits")
        flags.append(f"{clause} AS e{index}")
        flag_params.extend(params)

    selects = ", ".join(
        f"count(*) FILTER (WHERE y.e{index}) AS n{index}, "
        f"count(*) FILTER (WHERE y.e{index} AND y.llm_score IS NOT NULL) AS s{index}, "
        f"count(*) FILTER (WHERE y.e{index} AND y.llm_score >= 8) AS h{index}, "
        f"coalesce(sum(y.llm_score) FILTER (WHERE y.e{index}), 0) AS t{index}"
        for index in range(len(axes))
    )
    row = conn.execute(
        f"""
        WITH base AS MATERIALIZED (
            SELECT a.llm_score, a.title, a.title_en
            FROM articles a
            WHERE a.day >= %s AND a.day < %s
        ),
        hits AS MATERIALIZED (
            SELECT base.llm_score, base.title, base.title_en
            FROM base WHERE {normalized_text_sql("base")} ~ %s
        ),
        y AS MATERIALIZED (
            SELECT hits.llm_score, {", ".join(flags)} FROM hits
        )
        SELECT {selects} FROM y
        """,  # noqa: S608
        [month, month_end, econ_prefilter_pattern(), *flag_params],
    ).fetchone()
    row = row or {}

    for index, axis in enumerate(axes):
        conn.execute(
            """
            INSERT INTO rollup_monthly_econ
                (month, axis, article_count, scored_count, high_count, score_sum)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (month, axis) DO UPDATE SET
                article_count = EXCLUDED.article_count,
                scored_count  = EXCLUDED.scored_count,
                high_count    = EXCLUDED.high_count,
                score_sum     = EXCLUDED.score_sum
            """,
            (
                month,
                axis.key,
                int(row.get(f"n{index}") or 0),
                int(row.get(f"s{index}") or 0),
                int(row.get(f"h{index}") or 0),
                float(row.get(f"t{index}") or 0),
            ),
        )
    # Every axis is written every time (the aggregate always returns a
    # value, zero or not), so there is no stale-key zero-out pass to do
    # here -- same reasoning as the score bands and the watchlist above.
    # An axis REMOVED from econaxes.py does leave rows behind; the read
    # side selects by the current axis list, so those rows are inert
    # rather than wrong.
    return len(axes)


def _upsert_coverage(conn: DictConnection, month: date, month_end: date) -> int:
    """The "were we actually watching this month" row. See
    migrations/0021's header for why a publication-date chart is
    unreadable without it.

    Note the two different clocks: `article_count` and friends are keyed
    on publication month, while `collected_days` counts distinct dates
    the collector ran inside this calendar month regardless of what it
    published. `collected_count` is the intersection -- published in this
    month AND fetched in this month.
    """
    row = conn.execute(
        f"""
        SELECT
          count(*) AS article_count,
          count(*) FILTER (WHERE review_status != 'filtered_out') AS matched_count,
          count(*) FILTER (WHERE llm_score IS NOT NULL) AS scored_count,
          count(*) FILTER (
            WHERE date_trunc('month', fetched_at AT TIME ZONE '{DHAKA_TZ}')::date = %s
          ) AS collected_count
        FROM articles
        WHERE day >= %s AND day < %s
        """,  # noqa: S608 -- DHAKA_TZ is a module constant, never caller input
        (month, month, month_end),
    ).fetchone()
    row = row or {}

    days_row = conn.execute(
        f"""
        SELECT count(DISTINCT (fetched_at AT TIME ZONE '{DHAKA_TZ}')::date) AS n
        FROM articles
        WHERE (fetched_at AT TIME ZONE '{DHAKA_TZ}') >= %s
          AND (fetched_at AT TIME ZONE '{DHAKA_TZ}') < %s
        """,  # noqa: S608 -- as above
        (month, month_end),
    ).fetchone()

    conn.execute(
        """
        INSERT INTO rollup_monthly_coverage
            (month, article_count, matched_count, scored_count, collected_count, collected_days)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (month) DO UPDATE SET
            article_count   = EXCLUDED.article_count,
            matched_count   = EXCLUDED.matched_count,
            scored_count    = EXCLUDED.scored_count,
            collected_count = EXCLUDED.collected_count,
            collected_days  = EXCLUDED.collected_days
        """,
        (
            month,
            int(row.get("article_count") or 0),
            int(row.get("matched_count") or 0),
            int(row.get("scored_count") or 0),
            int(row.get("collected_count") or 0),
            int((days_row or {}).get("n") or 0),
        ),
    )
    return 1


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
        "watch": _upsert_watch(conn, month, month_end),
        "econ": _upsert_econ(conn, month, month_end),
        "coverage": _upsert_coverage(conn, month, month_end),
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
    """One-off backfill: recompute rollups for every historical month
    that has data in `articles`.

    This used to iterate one call per DISTINCT `day`, on the grounds
    that recomputing a month several times is idempotent and cheap. It
    is still idempotent, and it is no longer cheap: the real corpus has
    203 distinct days inside 15 months, and the watchlist aggregate
    (migrations/0021) is a regex pass over every article in the month,
    so the per-day loop did the same expensive scan up to 31 times for
    one month's answer and ran for many minutes. Since
    `compute_rollups_for_day` only ever normalizes its argument to the
    1st of the month anyway, deduplicating to distinct months produces
    byte-identical rollup rows in a fraction of the time.
    """
    rows = conn.execute(
        "SELECT DISTINCT date_trunc('month', day)::date AS m FROM articles WHERE day IS NOT NULL ORDER BY m"
    ).fetchall()
    days = [r["m"] for r in rows]
    months_seen: set[date] = set()
    for d in days:
        compute_rollups_for_day(conn, d)
        months_seen.add(_month_start(d))

    # migrations/0021's coverage row is keyed on a SECOND clock: it also
    # counts the days the collector ran inside a calendar month, whatever
    # those articles' publication dates were. A month can therefore have
    # collection activity and no articles published in it, and the
    # publication-date loop above would skip it entirely, leaving that
    # month looking unmonitored on the dashboard.
    fetch_rows = conn.execute(
        f"SELECT DISTINCT date_trunc('month', fetched_at AT TIME ZONE '{DHAKA_TZ}')::date AS m "  # noqa: S608
        "FROM articles WHERE fetched_at IS NOT NULL ORDER BY m"
    ).fetchall()
    for r in fetch_rows:
        if r["m"] not in months_seen:
            compute_rollups_for_month(conn, r["m"])
            months_seen.add(r["m"])
    conn.commit()
    # Two different numbers on purpose: `months_recomputed` can exceed
    # `publication_months`, because a month in which the collector ran
    # but nothing was PUBLISHED still needs its coverage row written.
    result = {"publication_months": len(days), "months_recomputed": len(months_seen)}
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
