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
"""

from __future__ import annotations

from typing import Any

from app.db.connection import DictConnection

PAGE_SIZE = 20


def search_articles(conn: DictConnection, *, q: str, page: int = 1) -> dict[str, Any]:
    q = (q or "").strip()
    corpus_row = conn.execute("SELECT count(*) AS n FROM articles").fetchone()
    corpus_size = int(corpus_row["n"]) if corpus_row else 0

    if not q:
        return {"results": [], "total": 0, "corpusSize": corpus_size}

    page = max(1, page)
    offset = (page - 1) * PAGE_SIZE

    total_row = conn.execute(
        "SELECT count(*) AS n FROM articles WHERE search_vec @@ plainto_tsquery('simple', %s)", (q,)
    ).fetchone()
    total = int(total_row["n"]) if total_row else 0

    rows = conn.execute(
        """
        SELECT a.id, a.source, a.day, a.title, a.title_en, a.url, a.review_status,
               COALESCE(ab.content_text, '') AS content_text
        FROM articles a
        LEFT JOIN article_bodies ab ON ab.article_id = a.id
        WHERE a.search_vec @@ plainto_tsquery('simple', %(q)s)
        ORDER BY ts_rank(a.search_vec, plainto_tsquery('simple', %(q)s)) DESC, a.id DESC
        LIMIT %(limit)s OFFSET %(offset)s
        """,
        {"q": q, "limit": PAGE_SIZE, "offset": offset},
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
    return {"results": results, "total": total, "corpusSize": corpus_size}
