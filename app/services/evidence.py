"""Board statistics and the articles behind them, for any predicate.

Both risk boards -- the telecom watchlist (`app/db/watchlist.py`) and
the economy axes (`app/db/econaxes.py`) -- ask the same two questions of
the same `articles` table: how much coverage did this line get and how
severe was it, and WHICH headlines are the severe ones. This module
answers both for any SQL boolean predicate, so neither board grows its
own copy and the two cannot drift into meaning different things.

That is the whole design constraint. `watchlist.entity_predicate_sql`
returns a bare SQL string, `econaxes.axis_where_sql` returns a string
plus bind parameters; the functions here take the `(where_sql, params)`
pair, so a caller passes `axis_where_sql(axis)` directly and passes
`(entity_predicate_sql(entity), [])` for a watchlist entity. Neither
board is privileged.

WHY THE COUNTS AND THE EVIDENCE ARE SEPARATE CALLS
──────────────────────────────────────────────────
`board_stats` is cheap and answers the trend question. `critical_items`
reads whole rows and is only ever wanted for the handful of headlines a
reader is about to look at. Keeping them apart means the board can
render its numbers without paying for text it may never show.

More importantly, they can legitimately DISAGREE, and the UI has to be
built knowing that:

RETENTION -- THE HONEST-EMPTY CASE
──────────────────────────────────
`app/services/storage.py` purges raw `articles` rows on the
`raw_retention_days` window (90 days by default; see
`app/db/settings.py`). The monthly rollups
(`app/db/rollups.py`) survive that purge by design -- they are bounded
per-month aggregates, which is the entire reason
`migrations/0007_rollups.sql` and `0021` exist and why the long-term
dashboard reads rollups and never raw articles.

`critical_items` reads LIVE `articles`. So for any month older than the
retention window it returns `[]` while the rollup row for that same
month still reports a real, correct `high_count`. That is not an error,
not an empty result to hide, and not a reason to suppress the number:
it is "we counted 12 critical articles that month and no longer keep
the headlines". A caller must render that as an explicit absence --
the count with a note that the articles have aged out -- rather than
silently showing zero evidence, or worse, treating the empty list as
evidence that the count is wrong.

`board_stats` reads live `articles` too, so it is subject to exactly
the same window. Callers wanting numbers that outlive retention read
the rollup tables instead; this module is the SHORT-TERM / drill-down
path, matching the split `app/api/routers/dashboard.py` already draws
between `dashboard_today` (live articles) and `/dashboard/trends`
(rollups only).

NEVER FABRICATE A REASON
────────────────────────
`EvidenceItem.reason` is the model's own one-line justification from
`articles.llm_reason`. When that column is empty -- an article scored
by a provider that returned no reason, or scored before the column was
populated -- this module returns an empty string and stops. It does not
substitute the headline, a topic tag, a template, or a generated
paraphrase. A reader who is told WHY an article is critical will act on
it, so an invented reason is worse than a blank one, and the blank is
what the caller must render.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

# The score bands the desk reads, from the scoring rubric in
# `app/services/llm/prompts.py` (RED 8-10, YELLOW 6-7, GREEN 5,
# MAGENTA 1-4). The same boundaries `app/db/rollups.py` writes into
# `rollup_monthly_score_band` as high/medium/low/least, so a band on
# this screen and a band on the trend chart mean the same thing.
_RED_MIN = 8.0
_YELLOW_MIN = 6.0
_GREEN_MIN = 5.0


@dataclass(frozen=True)
class BoardStats:
    items: int
    scored: int
    critical: int
    mean: float | None


@dataclass(frozen=True)
class EvidenceItem:
    article_id: int
    score: float
    band: str  # "red" | "yellow" | "green" | "magenta"
    title: str
    source: str
    day: date
    reason: str


def band_for(score: float) -> str:
    """Map an `llm_score` onto its colour band. See the rubric constants
    above; the boundaries are inclusive at the bottom of each band.
    """
    if score >= _RED_MIN:
        return "red"
    if score >= _YELLOW_MIN:
        return "yellow"
    if score >= _GREEN_MIN:
        return "green"
    return "magenta"


# `title_en` is the enrichment stage's English translation and is `''`
# (never NULL) when translation has not run, so "prefer the translation
# when there is one" is `nullif(...) -> coalesce`. This is the SQL form
# of the `title_en or title` convention the Python side already uses in
# app/services/brief.py, app/services/export.py and
# app/services/near_duplicates.py.
_TITLE_SQL = "coalesce(nullif(a.title_en, ''), a.title)"


def board_stats(conn: Any, where_sql: str, params: list[str], *, since: date) -> BoardStats:
    """Volume and severity for one board line since `since`.

    `items` counts every identified headline; `scored` how many of them
    the judge actually scored; `critical` how many reached the RED band.
    `mean` is over the scored subset and is None when nothing was
    scored -- deliberately not 0.0, because "no scored articles" and "a
    mean of zero" are different states and a caller must be able to tell
    them apart before drawing a trend line.
    """
    row = conn.execute(
        f"""
        SELECT count(*) AS items,
               count(a.llm_score) AS scored,
               count(*) FILTER (WHERE a.llm_score >= {_RED_MIN}) AS critical,
               avg(a.llm_score) AS mean
        FROM articles a
        WHERE a.day >= %s AND {where_sql}
        """,  # noqa: S608 -- where_sql is built by watchlist/econaxes from frozen module constants
        [since, *params],
    ).fetchone()
    row = row or {}
    mean = row.get("mean")
    return BoardStats(
        items=int(row.get("items") or 0),
        scored=int(row.get("scored") or 0),
        critical=int(row.get("critical") or 0),
        mean=float(mean) if mean is not None else None,
    )


def critical_items(
    conn: Any,
    where_sql: str,
    params: list[str],
    *,
    since: date,
    limit: int = 3,
    min_score: float = 8.0,
) -> list[EvidenceItem]:
    """The headlines behind `BoardStats.critical` -- worst first.

    Ordered by score descending then day descending, so the most severe
    article wins and the most recent breaks a tie. Returns `[]` when the
    month has aged past the raw-retention window; see the module
    docstring on why that is a real state to render, not an error.
    """
    rows = conn.execute(
        f"""
        SELECT a.id, a.llm_score, {_TITLE_SQL} AS title, a.source, a.day,
               coalesce(a.llm_reason, '') AS reason
        FROM articles a
        WHERE a.day >= %s AND a.llm_score >= %s AND {where_sql}
        ORDER BY a.llm_score DESC, a.day DESC, a.id DESC
        LIMIT %s
        """,  # noqa: S608 -- as above
        [since, min_score, *params, limit],
    ).fetchall()
    return [
        EvidenceItem(
            article_id=int(r["id"]),
            score=float(r["llm_score"]),
            band=band_for(float(r["llm_score"])),
            title=r["title"] or "",
            source=r["source"] or "",
            day=r["day"],
            # Empty stays empty -- the caller renders an honest absence.
            reason=(r["reason"] or "").strip(),
        )
        for r in rows
    ]
