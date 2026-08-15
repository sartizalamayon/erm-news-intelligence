"""Review queue: filter/search/sort matched articles; approve/reject/
star/annotate; duplicate-group and topic-cluster expansion; bulk actions.
See frontend-contract.md §3.3 and frontend/js/screens/review.js's header
comment for the exact shape this was built against.

Every route requires an authenticated session (`get_current_user`) —
this is a user-facing screen, not an admin one.

── PERF (REVIEW-REBUILD, measured against the local Postgres dev DB —
9,073 real pending rows) ──────────────────────────────────────────────

The previous version of this module built the FULL per-article shape
(dups, topicPeers, `content_text` body up to 4000 chars, note) for
EVERY row matching the filter, unconditionally, with no LIMIT — on the
theory that review.js never calls a separate per-id detail endpoint so
"one richer list query is the correct shape, not an oversight." That
reasoning didn't survive contact with real data volume. Measured,
in-process, end to end for `GET /articles?status=pending` (the
screen's default view) before this rewrite:

    fetch_rows (SQL):  501 ms  — ORDER BY llm_score DESC over 9,073 rows
                                 whose width includes the joined
                                 article_bodies.content_text spills the
                                 sort to disk (EXPLAIN ANALYZE: "Sort
                                 Method: external merge  Disk: 9688kB").
    enrich (Python):  1,273 ms — building 9,073 full ArticleOut objects
                                 (each with its own DupOut/TopicPeerOut
                                 lists and full body text).
    TOTAL:            1,774 ms backend time alone, before FastAPI JSON
                                 serialization or network transfer.
    JSON payload:      131.7 MB for that one response.

Root causes, in order of impact:
  1. No pagination/limit at all — every matching row, always.
  2. `content_text` (list-view doesn't render body) pulled into every
     row via the article_bodies JOIN, forcing the disk-spilled sort
     above (dropping just this column, with the same LIMIT-less query,
     already takes the DB side from 190ms to 10-23ms — see the
     LIMIT-150 EXPLAIN in the task's diagnosis notes).
  3. Full detail (dups/topicPeers/note/body) built in Python for every
     row, when only ~1 row (whichever is selected) is ever displayed
     at once.

Fix: the list query (`detail=False` below) never joins article_bodies
and never builds DupOut/TopicPeerOut/note/body — it returns the exact
fields review.js's card rows render, LIMITed server-side (default 200,
max 500, or `top_n` for the "top N by score" mode) with `total`/facet
counts computed via cheap grouped-count SQL instead of a full-row
Python loop. Full detail (body, dups, topicPeers) is now fetched
on-demand via the new `GET /articles/{id}` the instant a row is
selected — bounded by user clicks, not list size. Measured after the
rewrite, same DB, same 9,073-row pending set, default limit=200:
list query ~25ms DB + ~15ms enrich, ~180KB payload (see
tests/test_core_review.py's perf-shape assertions for the checked
invariants: list rows carry no body text, detail is a separate call).

Schema gap, flagged rather than worked around silently: `articles` has
no `starred` or `note` column (migrations/0003_articles.sql) — this
module was written against the schema as it exists today, without
touching migrations/** (ARCH-DB's territory). Per-article star/note
state is persisted in the generic `settings` key/value table
(`app.db.settings`, 0001_settings.sql) under `article_star::<id>` /
`article_note::<id>` keys — the same reuse-not-reinvent move ARCH-DB
itself made for the purge-preview snapshot (storage-policy.md §4.1).
REVIEW-REBUILD: the note feature itself was removed from the review
SCREEN (owner's explicit call — it's clutter nobody used); the
PATCH .../settings-table plumbing is left intact rather than deleted,
since this module doesn't own migrations and a future screen (or an
admin annotation view) may still want it. review.js simply no longer
sends or reads it.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status

from app.api.deps import AuthenticatedUser, get_current_user
from app.api.schemas.core import (
    ArticleListOut,
    ArticleOut,
    ArticlePatchIn,
    BulkActionIn,
    BulkActionOut,
    DayCountOut,
    DupOut,
    SourceFilterOut,
    TopicPeerOut,
)
from app.db.connection import get_connection

router = APIRouter(prefix="/articles", tags=["review"])

DHAKA = timezone(timedelta(hours=6))
VALID_STATUSES = ("pending", "approved", "rejected")

DEFAULT_LIMIT = 200
MAX_LIMIT = 500

# Mirrors app.services.matching.is_bengali's Unicode range (U+0980-U+09FF,
# "at least one Bengali-script character") without importing that
# module's private constants. Pushed into SQL (bound as a parameter, not
# string-interpolated) rather than filtered in Python post-fetch — the
# old code fetched every row THEN dropped non-matching ones in Python,
# which both defeats LIMIT/OFFSET pagination (a page could come back
# short or empty after the fact) and was part of the same
# fetch-everything pattern flagged in the PERF section above.
_BENGALI_RANGE_RE = f"[{chr(0x0980)}-{chr(0x09FF)}]"

# List rows: exactly what an article-row card renders (frontend/js/screens/
# review.js) — no body, no title_en/url/reason/keywords, no article_bodies
# join. See module PERF docstring.
SELECT_COLUMNS_LIST = (
    "a.id, a.source, a.title, a.day, a.published, a.fetched_at, "
    "a.sections, a.dup_group, a.review_status, a.llm_score"
)
FROM_LIST = "articles a"

# Detail rows: the full per-article shape, used only for a single
# selected article (GET /articles/{id}, PATCH, bulk's dup/keep/drop
# responses) — never for a list of many.
SELECT_COLUMNS_DETAIL = SELECT_COLUMNS_LIST + (
    ", a.url, a.title_en, a.llm_reason, a.keywords, a.topic_tag, a.body_status, "
    "COALESCE(ab.content_text, '') AS content_text"
)
FROM_DETAIL = "articles a LEFT JOIN article_bodies ab ON ab.article_id = a.id"

SORT_SQL = {
    "relevance": "a.llm_score DESC NULLS LAST, a.id DESC",
    "date": "COALESCE(a.published, a.fetched_at) DESC",
    "source": "a.source ASC, a.llm_score DESC NULLS LAST",
}


# ── settings-table star/note store (see module docstring) ───────────────


def _star_key(article_id: int) -> str:
    return f"article_star::{article_id}"


def _note_key(article_id: int) -> str:
    return f"article_note::{article_id}"


def _load_annotations(conn: Any, ids: list[int]) -> tuple[dict[int, bool], dict[int, str]]:
    if not ids:
        return {}, {}
    keys = [_star_key(i) for i in ids] + [_note_key(i) for i in ids]
    rows = conn.execute("SELECT key, value FROM settings WHERE key = ANY(%s)", (keys,)).fetchall()
    starred: dict[int, bool] = {}
    notes: dict[int, str] = {}
    for row in rows:
        key = row["key"]
        if key.startswith("article_star::"):
            starred[int(key.split("::", 1)[1])] = row["value"] == "true"
        elif key.startswith("article_note::"):
            notes[int(key.split("::", 1)[1])] = row["value"]
    return starred, notes


def _set_starred(conn: Any, article_id: int, value: bool) -> None:
    conn.execute(
        """
        INSERT INTO settings (key, value, updated_at) VALUES (%s, %s, now())
        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()
        """,
        (_star_key(article_id), "true" if value else "false"),
    )


def _set_note(conn: Any, article_id: int, note: str) -> None:
    if note:
        conn.execute(
            """
            INSERT INTO settings (key, value, updated_at) VALUES (%s, %s, now())
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()
            """,
            (_note_key(article_id), note),
        )
    else:
        conn.execute("DELETE FROM settings WHERE key = %s", (_note_key(article_id),))


# ── query building ────────────────────────────────────────────────────


def _build_where(
    *,
    status: str | None,
    tier_keys: list[str] | None,
    score_min: float | None,
    score_max: float | None,
    q: str | None,
    date_from: date | None,
    date_to: date | None,
    lang: str | None,
    exclude_sources: list[str] | None,
    starred_only: bool,
) -> tuple[str, dict[str, Any]]:
    from app.api.schemas.core import TIER_SQL

    clauses = ["a.review_status != 'filtered_out'", "COALESCE(a.is_primary, TRUE) = TRUE"]
    params: dict[str, Any] = {}
    if status:
        clauses.append("a.review_status = %(status)s")
        params["status"] = status
    if tier_keys:
        tier_clauses = [TIER_SQL[k] for k in tier_keys if k in TIER_SQL]
        if tier_clauses:
            clauses.append("a.llm_score IS NOT NULL AND (" + " OR ".join(tier_clauses) + ")")
    if score_min is not None:
        clauses.append("a.llm_score >= %(score_min)s")
        params["score_min"] = score_min
    if score_max is not None:
        clauses.append("a.llm_score <= %(score_max)s")
        params["score_max"] = score_max
    if q:
        clauses.append(
            "(a.title ILIKE %(q)s OR a.title_en ILIKE %(q)s OR a.source ILIKE %(q)s OR a.sections ILIKE %(q)s)"
        )
        params["q"] = f"%{q}%"
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
    if starred_only:
        clauses.append(
            "a.id IN (SELECT (split_part(key, '::', 2))::bigint FROM settings "
            "WHERE key LIKE %(star_prefix)s AND value = 'true')"
        )
        params["star_prefix"] = "article_star::%"
    if exclude_sources:
        clauses.append("NOT (a.source = ANY(%(excl)s))")
        params["excl"] = list(exclude_sources)
    return " AND ".join(clauses), params


def _fetch_rows(
    conn: Any,
    *,
    ids: list[int] | None = None,
    status: str | None = None,
    tier_keys: list[str] | None = None,
    score_min: float | None = None,
    score_max: float | None = None,
    q: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    lang: str | None = None,
    exclude_sources: list[str] | None = None,
    starred_only: bool = False,
    sort: str = "relevance",
    limit: int | None = None,
    offset: int = 0,
    detail: bool = True,
) -> list[dict[str, Any]]:
    where, params = _build_where(
        status=status,
        tier_keys=tier_keys,
        score_min=score_min,
        score_max=score_max,
        q=q,
        date_from=date_from,
        date_to=date_to,
        lang=lang,
        exclude_sources=exclude_sources,
        starred_only=starred_only,
    )
    if ids is not None:
        where += " AND a.id = ANY(%(ids)s)"
        params["ids"] = list(ids)
    order = SORT_SQL.get(sort, SORT_SQL["relevance"])
    cols = SELECT_COLUMNS_DETAIL if detail else SELECT_COLUMNS_LIST
    from_clause = FROM_DETAIL if detail else FROM_LIST
    sql = f"SELECT {cols} FROM {from_clause} WHERE {where} ORDER BY {order}"
    if limit is not None:
        sql += " LIMIT %(limit)s"
        params["limit"] = limit
    if offset:
        sql += " OFFSET %(offset)s"
        params["offset"] = offset
    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def _fmt_time(published: datetime | None, fetched_at: datetime | None) -> str:
    dt = published or fetched_at
    if dt is None:
        return ""
    return dt.astimezone(DHAKA).strftime("%H:%M")


def _fmt_date(day: date | None) -> str:
    if day is None:
        return ""
    return day.isoformat()


def _primary_section(sections: str) -> str:
    if not sections:
        return ""
    return sections.split(", ")[0]


def _enrich_and_build(conn: Any, rows: list[dict[str, Any]], *, detail: bool = True) -> list[ArticleOut]:
    """Batches the extra lookups every ArticleOut needs (dup siblings,
    topic peers, star/note) into a handful of queries regardless of
    list size, rather than one round-trip per row.

    `detail=False` (the list path) skips the topic-peers query entirely
    and omits body/dups/topicPeers/note/titleEn/url/reason/keywords from
    the output — those fields simply aren't in `rows` to begin with
    (list callers pass SELECT_COLUMNS_LIST), and the card list never
    renders them. `dupCount` is still computed accurately either way
    since knowing "how many other outlets" is part of the card. See the
    module's PERF docstring for why this split exists.
    """
    dup_groups = sorted({r["dup_group"] for r in rows if r.get("dup_group") is not None})
    dup_siblings: dict[Any, list[dict[str, Any]]] = {}
    if dup_groups:
        sib_rows = conn.execute(
            "SELECT id, source, title, url, dup_group FROM articles WHERE dup_group = ANY(%s)", (dup_groups,)
        ).fetchall()
        for r in sib_rows:
            dup_siblings.setdefault(r["dup_group"], []).append(dict(r))

    topic_peers: dict[str, list[dict[str, Any]]] = {}
    if detail:
        topic_tags = sorted({r["topic_tag"] for r in rows if r.get("topic_tag")})
        if topic_tags:
            peer_rows = conn.execute(
                "SELECT id, source, title, llm_score, topic_tag FROM articles "
                "WHERE topic_tag = ANY(%s) AND topic_standout = TRUE",
                (topic_tags,),
            ).fetchall()
            for r in peer_rows:
                topic_peers.setdefault(r["topic_tag"], []).append(dict(r))

    ids = [r["id"] for r in rows]
    starred_map, note_map = _load_annotations(conn, ids)

    out: list[ArticleOut] = []
    for r in rows:
        rid = r["id"]
        siblings: list[dict[str, Any]] = []
        if r.get("dup_group") is not None:
            siblings = [s for s in dup_siblings.get(r["dup_group"], []) if s["id"] != rid]
        dups: list[DupOut] | None = None
        if detail:
            dups = [DupOut(id=s["id"], source=s["source"], title=s["title"], url=s.get("url") or "") for s in siblings]

        topic = (r.get("topic_tag") or "") if detail else ""
        peers: list[TopicPeerOut] | None = None
        if detail and topic:
            plist = [p for p in topic_peers.get(topic, []) if p["id"] != rid]
            peers = [
                TopicPeerOut(id=p["id"], source=p["source"], title=p["title"], score=p.get("llm_score"))
                for p in plist
            ]

        out.append(
            ArticleOut(
                id=rid,
                title=r["title"],
                source=r["source"],
                date=_fmt_date(r.get("day")),
                time=_fmt_time(r.get("published"), r.get("fetched_at")),
                section=_primary_section(r.get("sections") or ""),
                score=r.get("llm_score"),
                dupCount=len(siblings),
                isStarred=starred_map.get(rid, False),
                status=r["review_status"],
                titleEn=(r.get("title_en") or "") if detail else "",
                url=(r.get("url") or "") if detail else "",
                reason=(r.get("llm_reason") or "") if detail else "",
                keywords=[k for k in (r.get("keywords") or "").split(", ") if k] if detail else [],
                body=(r.get("content_text") or "") if detail else "",
                bodyFetched=(r.get("body_status") == "fetched") if detail else False,
                note=note_map.get(rid, "") if detail else "",
                dups=dups,
                topicPeers=peers,
                topic=topic,
            )
        )
    return out


# ── routes ────────────────────────────────────────────────────────────


@router.get("", response_model=ArticleListOut)
def list_articles(
    q: str | None = None,
    sort: str = "relevance",
    tier: str | None = None,
    lang: str | None = None,
    exclude_source: str | None = None,
    status: str | None = None,
    starred: str | None = None,
    score_min: float | None = None,
    score_max: float | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    top_n: int | None = None,
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
    user: AuthenticatedUser = Depends(get_current_user),
) -> ArticleListOut:
    tier_keys = [t for t in tier.split(",") if t] if tier else None
    excluded = [s for s in (exclude_source.split(",") if exclude_source else []) if s]
    starred_only = starred == "true"

    # "Top N by AI score" is a distinct sort/limit mode (owner requirement):
    # it forces relevance ordering and overrides normal limit/offset
    # paging, but stays combinable with every other filter above.
    if top_n:
        eff_sort = "relevance"
        eff_limit = max(1, min(top_n, MAX_LIMIT))
        eff_offset = 0
    else:
        eff_sort = sort
        eff_limit = max(1, min(limit, MAX_LIMIT))
        eff_offset = max(0, offset)

    with get_connection() as conn:
        # Facet counts: every active filter EXCEPT exclude_source and
        # starred (classic faceted-search pattern — a source's own count,
        # and the total-vs-starred split, must stay visible even once
        # you've filtered by it, or the sidebar/toggle can't be
        # un-toggled sanely). Computed via a grouped SQL aggregate, never
        # by pulling full rows into Python — see module PERF docstring.
        facet_where, facet_params = _build_where(
            status=status,
            tier_keys=tier_keys,
            score_min=score_min,
            score_max=score_max,
            q=q,
            date_from=date_from,
            date_to=date_to,
            lang=lang,
            exclude_sources=None,
            starred_only=False,
        )
        facet_rows = conn.execute(
            f"SELECT a.source AS source, count(*) AS n FROM {FROM_LIST} WHERE {facet_where} GROUP BY a.source",
            facet_params,
        ).fetchall()
        sources = [
            SourceFilterOut(name=r["source"], count=r["n"]) for r in sorted(facet_rows, key=lambda r: -r["n"])
        ]

        # `total`: how many rows match ALL active filters (including
        # exclude_source/starred) — used by the FE for "N of Total" and
        # to decide whether a "Load more" affordance should show.
        full_where, full_params = _build_where(
            status=status,
            tier_keys=tier_keys,
            score_min=score_min,
            score_max=score_max,
            q=q,
            date_from=date_from,
            date_to=date_to,
            lang=lang,
            exclude_sources=excluded,
            starred_only=starred_only,
        )
        total_row = conn.execute(f"SELECT count(*) AS n FROM {FROM_LIST} WHERE {full_where}", full_params).fetchone()
        total = total_row["n"] if total_row else 0

        # TRUE per-day counts, same filters as `total`, independent of
        # paging. The list groups by date, and its headers previously
        # counted only the rows on the loaded page — "44" on a day that
        # really had 543. One grouped aggregate, not a per-day query.
        day_rows = conn.execute(
            f"SELECT a.day AS d, count(*) AS n FROM {FROM_LIST} WHERE {full_where} GROUP BY a.day ORDER BY a.day DESC",
            full_params,
        ).fetchall()
        days = [DayCountOut(date=r["d"].isoformat() if r["d"] else "", count=r["n"]) for r in day_rows]

        rows = _fetch_rows(
            conn,
            status=status,
            tier_keys=tier_keys,
            score_min=score_min,
            score_max=score_max,
            q=q,
            date_from=date_from,
            date_to=date_to,
            lang=lang,
            exclude_sources=excluded,
            starred_only=starred_only,
            sort=eff_sort,
            limit=eff_limit,
            offset=eff_offset,
            detail=False,
        )
        items = _enrich_and_build(conn, rows, detail=False)

    return ArticleListOut(items=items, total=total, sources=sources, days=days)


@router.get("/{article_id}", response_model=ArticleOut)
def get_article(article_id: int, user: AuthenticatedUser = Depends(get_current_user)) -> ArticleOut:
    """On-demand full detail for a single article (body, dups,
    topicPeers, note) — fetched the instant review.js selects a card,
    never for the whole list. See module PERF docstring.
    """
    with get_connection() as conn:
        rows = _fetch_rows(conn, ids=[article_id], detail=True)
        if not rows:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="article not found")
        items = _enrich_and_build(conn, rows, detail=True)
    return items[0]


@router.patch("/{article_id}", response_model=ArticleOut)
def patch_article(
    article_id: int, body: ArticlePatchIn, user: AuthenticatedUser = Depends(get_current_user)
) -> ArticleOut:
    with get_connection() as conn:
        exists = conn.execute("SELECT id FROM articles WHERE id = %s", (article_id,)).fetchone()
        if exists is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="article not found")

        if body.status is not None:
            if body.status not in VALID_STATUSES:
                raise HTTPException(status_code=422, detail=f"status must be one of {VALID_STATUSES}")
            conn.execute(
                "UPDATE articles SET review_status = %s, reviewed_by = %s, reviewed_at = now() WHERE id = %s",
                (body.status, user.email, article_id),
            )
        if body.starred is not None:
            _set_starred(conn, article_id, body.starred)
        if body.note is not None:
            _set_note(conn, article_id, body.note)

        rows = _fetch_rows(conn, ids=[article_id], detail=True)
        if not rows:
            # The row exists but no longer matches the base WHERE (e.g. it
            # was somehow filtered_out) — fall back to a direct fetch so a
            # successful write never 404s on its own response.
            raw = conn.execute(
                f"SELECT {SELECT_COLUMNS_DETAIL} FROM {FROM_DETAIL} WHERE a.id = %s", (article_id,)
            ).fetchall()
            rows = [dict(r) for r in raw]
        items = _enrich_and_build(conn, rows, detail=True)
    return items[0]


@router.post("/bulk", response_model=BulkActionOut)
def bulk_action(body: BulkActionIn, user: AuthenticatedUser = Depends(get_current_user)) -> BulkActionOut:
    if not body.ids:
        return BulkActionOut(updated=0)
    if body.action not in ("approve", "reject", "star"):
        raise HTTPException(status_code=422, detail="action must be one of approve, reject, star")
    with get_connection() as conn:
        if body.action in ("approve", "reject"):
            new_status = "approved" if body.action == "approve" else "rejected"
            conn.execute(
                "UPDATE articles SET review_status = %s, reviewed_by = %s, reviewed_at = now() WHERE id = ANY(%s)",
                (new_status, user.email, body.ids),
            )
        else:
            for article_id in body.ids:
                _set_starred(conn, article_id, True)
    return BulkActionOut(updated=len(body.ids))


def _group_members(conn: Any, dup_group: Any) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT id, source FROM articles WHERE dup_group = %s", (dup_group,)).fetchall()
    return [dict(r) for r in rows]


@router.post("/{article_id}/dups/{dup_id}/keep", response_model=ArticleOut)
def keep_dup(article_id: int, dup_id: int, user: AuthenticatedUser = Depends(get_current_user)) -> ArticleOut:
    with get_connection() as conn:
        cur = conn.execute("SELECT dup_group FROM articles WHERE id = %s", (article_id,)).fetchone()
        if cur is None or cur["dup_group"] is None:
            raise HTTPException(status_code=404, detail="article not found or has no duplicate group")
        dup_group = cur["dup_group"]
        dup_row = conn.execute(
            "SELECT id FROM articles WHERE id = %s AND dup_group = %s", (dup_id, dup_group)
        ).fetchone()
        if dup_row is None:
            raise HTTPException(status_code=404, detail="duplicate not found in this article's group")

        members = _group_members(conn, dup_group)
        for m in members:
            conn.execute("UPDATE articles SET is_primary = %s WHERE id = %s", (m["id"] == dup_id, m["id"]))
        others = sorted(m["source"] for m in members if m["id"] != dup_id)
        conn.execute("UPDATE articles SET also_in = %s WHERE id = %s", (", ".join(others), dup_id))
        for m in members:
            if m["id"] != dup_id:
                conn.execute("UPDATE articles SET also_in = '' WHERE id = %s", (m["id"],))

        rows = _fetch_rows(conn, ids=[dup_id], detail=True)
        items = _enrich_and_build(conn, rows, detail=True)
    if not items:
        raise HTTPException(status_code=404, detail="article not found")
    return items[0]


@router.post("/{article_id}/dups/{dup_id}/drop", response_model=ArticleOut)
def drop_dup(article_id: int, dup_id: int, user: AuthenticatedUser = Depends(get_current_user)) -> ArticleOut:
    with get_connection() as conn:
        cur = conn.execute("SELECT dup_group FROM articles WHERE id = %s", (article_id,)).fetchone()
        if cur is None or cur["dup_group"] is None:
            raise HTTPException(status_code=404, detail="article not found or has no duplicate group")
        dup_group = cur["dup_group"]
        dup_row = conn.execute(
            "SELECT id FROM articles WHERE id = %s AND dup_group = %s", (dup_id, dup_group)
        ).fetchone()
        if dup_row is None:
            raise HTTPException(status_code=404, detail="duplicate not found in this article's group")

        conn.execute(
            "UPDATE articles SET dup_group = NULL, is_primary = TRUE, also_in = '' WHERE id = %s", (dup_id,)
        )
        remaining = [m for m in _group_members(conn, dup_group) if m["id"] != dup_id]
        if len(remaining) <= 1:
            for m in remaining:
                conn.execute(
                    "UPDATE articles SET dup_group = NULL, is_primary = TRUE, also_in = '' WHERE id = %s", (m["id"],)
                )
        else:
            primary = conn.execute(
                "SELECT id FROM articles WHERE dup_group = %s AND is_primary = TRUE", (dup_group,)
            ).fetchone()
            if primary is not None:
                others = sorted(m["source"] for m in remaining if m["id"] != primary["id"])
                conn.execute("UPDATE articles SET also_in = %s WHERE id = %s", (", ".join(others), primary["id"]))

        rows = _fetch_rows(conn, ids=[article_id], detail=True)
        items = _enrich_and_build(conn, rows, detail=True)
    if not items:
        raise HTTPException(status_code=404, detail="article not found")
    return items[0]
