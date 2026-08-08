"""Full-text search archive (frontend-contract.md §3.6): headline,
English translation, and body text, across every collected article
regardless of date or review status.

Reads `articles.search_vec` (a GENERATED tsvector column, GIN-indexed as
`idx_articles_search_vec`, migrations/0003_articles.sql) via
`plainto_tsquery('simple', ...)` — the same `'simple'` text-search
config the index was built with (no English/Bengali-specific stemming,
deliberate per that migration's own comment: the corpus is 72.4%
Bengali-titled and Postgres ships no Bengali dictionary). Never
reimplements the index or its query shape — this module just calls it.

── PERF (SEARCH-PERF, measured against the local Postgres dev DB —
51,886 real articles, 7,511 real bodies) ───────────────────────────────

Diagnosis before fixing (owner's ask): is the GIN index used? Is body
text over-fetched? Is there a LIMIT? Does the frontend fire on every
keystroke?

  * GIN index: YES, correctly used — `EXPLAIN ANALYZE` on the original
    query shows `Bitmap Index Scan on idx_articles_search_vec`, not a
    seq scan. Not the bottleneck.
  * LIMIT: already present (PAGE_SIZE=20 below) — not the bottleneck.
  * Keystroke firing: frontend/js/screens/search.js only searches on
    Enter/click/suggestion-chip, never on `input` — not the bottleneck.
  * Body over-fetch: THIS was the measured cause. The original query
    did `articles LEFT JOIN article_bodies ... ORDER BY ts_rank(...)
    LIMIT 20` — Postgres must join article_bodies (a Seq Scan, ~7,511
    rows) against EVERY row matching the tsquery (not just the 20
    returned), because the join happens before the ORDER BY/LIMIT.
    Measured for a mid-frequency term ("bangladesh", 1,610 matches):

        original (join-then-limit):   ~30ms DB-side, EXPLAIN ANALYZE
                                       shows "Hash Right Join" over the
                                       full article_bodies seq scan.
        fixed (limit-then-join):      ~10.8ms DB-side — a Nested Loop
                                       using article_bodies_pkey, run
                                       only for the 20 returned rows.

    Fix: sort/paginate in a subquery on `articles` alone (ts_rank
    doesn't depend on body text), THEN left-join article_bodies onto
    just that page. This scales with PAGE_SIZE, not with how many rows
    match the search term — the gap only widens as the corpus and
    article_bodies grow, and as filters below narrow/widen the match
    count.

SEARCH-FILTERS: added combinable filters (date range, source
exclude-list, language, review status) mirroring app/api/routers/
review.py's `_build_where` pattern for consistency: a `sources` facet
(name + count) is computed off the same WHERE minus the source filter
itself, so unchecking a source doesn't make it vanish from the picker —
same reasoning as review.py's module docstring. `status` here is a
distinct vocabulary from `articles.review_status`'s raw values because
the owner's ask ("search covers not-keyword-matched-and-thus-not-
scored articles too") requires an explicit "never matched" option,
which maps to `review_status = 'filtered_out'` (verified against the
real corpus: every `filtered_out` row has `match_type = ''`, i.e. it
never passed keyword matching in the first place — see
app/services/matching.py's `review_status_for`/`match_type_for`).
"""

from __future__ import annotations

from datetime import date
from typing import Any

from app.db.connection import DictConnection

PAGE_SIZE = 20

# Mirrors app/api/routers/review.py's _BENGALI_RANGE_RE (Unicode range
# U+0980-U+09FF) — duplicated rather than imported so this module
# doesn't reach into review.py's private constants (same reasoning
# review.py itself gives for not importing app.services.matching's).
_BENGALI_RANGE_RE = f"[{chr(0x0980)}-{chr(0x09FF)}]"

# search `status` filter -> articles.review_status. "matched" = keyword/
# semantic-matched, awaiting a decision; "never_matched" = the filtered-
# out-before-scoring pool (see module docstring). Unknown/absent values
# apply no status filter at all ("all").
STATUS_TO_REVIEW_STATUS: dict[str, str] = {
    "matched": "pending",
    "approved": "approved",
    "rejected": "rejected",
    "never_matched": "filtered_out",
}


def _build_base_where(
    *,
    q: str,
    date_from: date | None,
    date_to: date | None,
    lang: str | None,
    status: str | None,
) -> tuple[str, dict[str, Any]]:
    """Every filter EXCEPT the source exclude-list — used both for the
    sources facet (which must stay stable as sources get toggled) and,
    with the exclude-list clause appended, for the real result set.
    """
    clauses = ["a.search_vec @@ plainto_tsquery('simple', %(q)s)"]
    params: dict[str, Any] = {"q": q}
    if date_from is not None:
        clauses.append("a.day >= %(date_from)s")
        params["date_from"] = date_from
    if date_to is not None:
        clauses.append("a.day <= %(date_to)s")
        params["date_to"] = date_to
    if lang == "en":
        clauses.append("NOT (a.title ~ %(bn_re)s)")
        params["bn_re"] = _BENGALI_RANGE_RE
    elif lang == "bn":
        clauses.append("a.title ~ %(bn_re)s")
        params["bn_re"] = _BENGALI_RANGE_RE
    review_status = STATUS_TO_REVIEW_STATUS.get(status or "")
    if review_status is not None:
        clauses.append("a.review_status = %(review_status)s")
        params["review_status"] = review_status
    return " AND ".join(clauses), params


def search_articles(
    conn: DictConnection,
    *,
    q: str,
    page: int = 1,
    date_from: date | None = None,
    date_to: date | None = None,
    lang: str | None = None,
    status: str | None = None,
    exclude_sources: list[str] | None = None,
) -> dict[str, Any]:
    q = (q or "").strip()
    corpus_row = conn.execute("SELECT count(*) AS n FROM articles").fetchone()
    corpus_size = int(corpus_row["n"]) if corpus_row else 0

    if not q:
        return {"results": [], "total": 0, "corpusSize": corpus_size, "sources": []}

    page = max(1, page)
    offset = (page - 1) * PAGE_SIZE

    base_where, base_params = _build_base_where(q=q, date_from=date_from, date_to=date_to, lang=lang, status=status)

    # Sources facet: same filters, minus the source exclude-list itself
    # (classic faceted-search rule — see module docstring / review.py).
    facet_rows = conn.execute(
        f"SELECT a.source AS source, count(*) AS n FROM articles a WHERE {base_where} GROUP BY a.source",
        base_params,
    ).fetchall()
    sources = [{"name": r["source"], "count": r["n"]} for r in sorted(facet_rows, key=lambda r: -r["n"])]

    full_where = base_where
    full_params = dict(base_params)
    if exclude_sources:
        full_where += " AND NOT (a.source = ANY(%(excl)s))"
        full_params["excl"] = list(exclude_sources)

    total_row = conn.execute(f"SELECT count(*) AS n FROM articles a WHERE {full_where}", full_params).fetchone()
    total = int(total_row["n"]) if total_row else 0

    page_params = dict(full_params)
    page_params["limit"] = PAGE_SIZE
    page_params["offset"] = offset

    # Join-after-limit (see module PERF docstring): rank/filter/sort/
    # paginate on `articles` alone first — ts_rank never needs body
    # text — then join article_bodies onto only the page actually
    # returned, instead of every matching row.
    rows = conn.execute(
        f"""
        SELECT sub.id, sub.source, sub.day, sub.title, sub.title_en, sub.url, sub.review_status,
               COALESCE(ab.content_text, '') AS content_text
        FROM (
            SELECT a.id, a.source, a.day, a.title, a.title_en, a.url, a.review_status,
                   ts_rank(a.search_vec, plainto_tsquery('simple', %(q)s)) AS rank
            FROM articles a
            WHERE {full_where}
            ORDER BY rank DESC, a.id DESC
            LIMIT %(limit)s OFFSET %(offset)s
        ) sub
        LEFT JOIN article_bodies ab ON ab.article_id = sub.id
        ORDER BY sub.rank DESC, sub.id DESC
        """,
        page_params,
    ).fetchall()

    results = []
    for row in rows:
        results.append(
            {
                "id": row["id"],
                "source": row["source"],
                "day": row["day"].isoformat() if row["day"] else "",
                "title": row["title"],
                "titleEn": row["title_en"] or "",
                "url": row["url"] or "",
                "status": row["review_status"] if row["review_status"] in ("approved", "rejected") else "",
                "body": row["content_text"] or "",
            }
        )
    return {"results": results, "total": total, "corpusSize": corpus_size, "sources": sources}
