"""Review queue: filter/search/sort matched articles; approve/reject/
star/annotate; duplicate-group and topic-cluster expansion; bulk actions.
See frontend-contract.md §3.3 and frontend/js/screens/review.js's header
comment for the exact shape this was built against.

Every route requires an authenticated session (`get_current_user`) —
this is a user-facing screen, not an admin one.

The list endpoint (`GET /articles`) returns the FULL per-article shape
(dups, topicPeers, body, keywords, note, ...) for every row, not a
lightweight summary: review.js never calls a separate per-id detail
endpoint — it selects the detail pane's `cur` article straight out of
the already-fetched list (`s.items.find(a => a.id === s.selId)`). One
richer list query is therefore the correct shape, not an oversight.

Schema gap, flagged rather than worked around silently: `articles` has
no `starred` or `note` column (migrations/0003_articles.sql) — this
module was written against the schema as it exists today, without
touching migrations/** (ARCH-DB's territory, and 0008/0009 are already
claimed by AUTH-OTP/the owner's admin seed). Per-article star/note state
is persisted in the generic `settings` key/value table
(`app.db.settings`, 0001_settings.sql) under `article_star::<id>` /
`article_note::<id>` keys — the same reuse-not-reinvent move ARCH-DB
itself made for the purge-preview snapshot (storage-policy.md §4.1).
Flagged back to ARCH-DB: dedicated `articles.starred BOOLEAN` /
`articles.note TEXT` columns would be the cleaner long-term home once a
migration slot opens.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status

from app.api.deps import AuthenticatedUser, get_current_user
from app.api.schemas.core import (
    ArticleListOut,
    ArticleOut,
    ArticlePatchIn,
    BulkActionIn,
    BulkActionOut,
)
from app.db.connection import get_connection
from app.services.matching import is_bengali

router = APIRouter(prefix="/articles", tags=["review"])

DHAKA = timezone(timedelta(hours=6))
VALID_STATUSES = ("pending", "approved", "rejected")

SELECT_COLUMNS = (
    "a.id, a.url, a.source, a.title, a.title_en, a.published, a.fetched_at, "
    "a.sections, a.keywords, a.dup_group, a.also_in, a.review_status, "
    "a.llm_score, a.llm_reason, a.topic_tag, a.body_status, "
    "COALESCE(ab.content_text, '') AS content_text"
)
FROM_CLAUSE = "articles a LEFT JOIN article_bodies ab ON ab.article_id = a.id"

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


def _build_where(*, status: str | None, tier_keys: list[str] | None, q: str | None) -> tuple[str, dict[str, Any]]:
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
    if q:
        clauses.append("(a.title ILIKE %(q)s OR a.title_en ILIKE %(q)s OR a.source ILIKE %(q)s OR a.sections ILIKE %(q)s)")
        params["q"] = f"%{q}%"
    return " AND ".join(clauses), params


def _fetch_rows(
    conn: Any, *, ids: list[int] | None = None, status: str | None = None, tier_keys: list[str] | None = None,
    q: str | None = None, sort: str = "relevance",
) -> list[dict[str, Any]]:
    where, params = _build_where(status=status, tier_keys=tier_keys, q=q)
    if ids is not None:
        where += " AND a.id = ANY(%(ids)s)"
        params["ids"] = list(ids)
    order = SORT_SQL.get(sort, SORT_SQL["relevance"])
    rows = conn.execute(f"SELECT {SELECT_COLUMNS} FROM {FROM_CLAUSE} WHERE {where} ORDER BY {order}", params).fetchall()
    return [dict(r) for r in rows]


def _fmt_time(published: datetime | None, fetched_at: datetime | None) -> str:
    dt = published or fetched_at
    if dt is None:
        return ""
    return dt.astimezone(DHAKA).strftime("%H:%M")


def _primary_section(sections: str) -> str:
    if not sections:
        return ""
    return sections.split(", ")[0]


def _enrich_and_build(conn: Any, rows: list[dict[str, Any]]) -> list[ArticleOut]:
    """Batches the extra lookups every full ArticleOut needs (dup
    siblings, topic peers, star/note) into a handful of queries
    regardless of list size, rather than one round-trip per row.
    """
    dup_groups = sorted({r["dup_group"] for r in rows if r.get("dup_group") is not None})
    dup_siblings: dict[Any, list[dict[str, Any]]] = {}
    if dup_groups:
        sib_rows = conn.execute(
            "SELECT id, source, title, url, dup_group FROM articles WHERE dup_group = ANY(%s)", (dup_groups,)
        ).fetchall()
        for r in sib_rows:
            dup_siblings.setdefault(r["dup_group"], []).append(dict(r))

    topic_tags = sorted({r["topic_tag"] for r in rows if r.get("topic_tag")})
    topic_peers: dict[str, list[dict[str, Any]]] = {}
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
        dups = None
        if r.get("dup_group") is not None:
            siblings = [s for s in dup_siblings.get(r["dup_group"], []) if s["id"] != rid]
            dups = [{"id": s["id"], "source": s["source"], "title": s["title"], "url": s.get("url") or ""} for s in siblings]
        peers = None
        topic = r.get("topic_tag") or ""
        if topic:
            plist = [p for p in topic_peers.get(topic, []) if p["id"] != rid]
            peers = [{"id": p["id"], "source": p["source"], "title": p["title"], "score": p.get("llm_score")} for p in plist]
        out.append(
            ArticleOut(
                id=rid,
                title=r["title"],
                source=r["source"],
                time=_fmt_time(r.get("published"), r.get("fetched_at")),
                section=_primary_section(r.get("sections") or ""),
                score=r.get("llm_score"),
                dupCount=len(dups) if dups is not None else 0,
                isStarred=starred_map.get(rid, False),
                status=r["review_status"],
                titleEn=r.get("title_en") or "",
                url=r.get("url") or "",
                reason=r.get("llm_reason") or "",
                keywords=[k for k in (r.get("keywords") or "").split(", ") if k],
                body=r.get("content_text") or "",
                bodyFetched=r.get("body_status") == "fetched",
                note=note_map.get(rid, ""),
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
    user: AuthenticatedUser = Depends(get_current_user),
) -> ArticleListOut:
    tier_keys = [t for t in tier.split(",") if t] if tier else None
    with get_connection() as conn:
        rows = _fetch_rows(conn, status=status, tier_keys=tier_keys, q=q, sort=sort)

        if lang == "en":
            rows = [r for r in rows if not is_bengali(r["title"])]
        elif lang == "bn":
            rows = [r for r in rows if is_bengali(r["title"])]

        source_counts: dict[str, int] = {}
        for r in rows:
            source_counts[r["source"]] = source_counts.get(r["source"], 0) + 1
        sources = [{"name": name, "count": n} for name, n in sorted(source_counts.items(), key=lambda kv: -kv[1])]

        excluded = {s for s in (exclude_source.split(",") if exclude_source else []) if s}
        if excluded:
            rows = [r for r in rows if r["source"] not in excluded]

        if starred == "true":
            ids = [r["id"] for r in rows]
            starred_map, _ = _load_annotations(conn, ids)
            rows = [r for r in rows if starred_map.get(r["id"], False)]

        items = _enrich_and_build(conn, rows)

    return ArticleListOut(items=items, total=len(items), sources=[dict(s) for s in sources])


@router.patch("/{article_id}", response_model=ArticleOut)
def patch_article(article_id: int, body: ArticlePatchIn, user: AuthenticatedUser = Depends(get_current_user)) -> ArticleOut:
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

        rows = _fetch_rows(conn, ids=[article_id])
        if not rows:
            # The row exists but no longer matches the base WHERE (e.g. it
            # was somehow filtered_out) — fall back to a direct fetch so a
            # successful write never 404s on its own response.
            raw = conn.execute(f"SELECT {SELECT_COLUMNS} FROM {FROM_CLAUSE} WHERE a.id = %s", (article_id,)).fetchall()
            rows = [dict(r) for r in raw]
        items = _enrich_and_build(conn, rows)
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
        dup_row = conn.execute("SELECT id FROM articles WHERE id = %s AND dup_group = %s", (dup_id, dup_group)).fetchone()
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

        rows = _fetch_rows(conn, ids=[dup_id])
        items = _enrich_and_build(conn, rows)
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
        dup_row = conn.execute("SELECT id FROM articles WHERE id = %s AND dup_group = %s", (dup_id, dup_group)).fetchone()
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

        rows = _fetch_rows(conn, ids=[article_id])
        items = _enrich_and_build(conn, rows)
    if not items:
        raise HTTPException(status_code=404, detail="article not found")
    return items[0]
