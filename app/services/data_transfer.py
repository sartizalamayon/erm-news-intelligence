"""Export / import — the data-lifecycle feature ADR §9.2 requires:
"export prior data -> delete to reclaim space -> re-import later if
needed." Export is what makes `app.services.storage`'s
`purge_matched_articles(require_exported=True)` a safe release valve
rather than a one-way data-loss button.

Format: gzip-compressed, newline-delimited JSON (JSONL). One JSON
object per line, tagged with a `"_table"` key so a single export file
carries both `articles` rows and their `article_bodies` rows together,
self-contained — DoD #6 requires "export then re-import must reproduce
equivalent rows," and an article whose body silently didn't travel with
it (or vice versa) would not be equivalent.

Justified in one sentence (DoD #6): JSONL preserves every column's real
type (nullable timestamps, booleans, ids that must round-trip exactly
for `topic_parent_id`/`dup_group` referential integrity) without a
separate schema file, streams row-by-row so memory use is constant
regardless of corpus size, and needs nothing beyond Python's stdlib
`json`/`gzip` to produce or consume — CSV would silently collapse NULL
and empty-string into the same thing and has no native way to carry a
nested/typed value.
"""

from __future__ import annotations

import gzip
import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from app.db.connection import DictConnection

ARTICLE_COLUMNS = (
    "id url url_norm source title title_en published fetched_at day sections keywords "
    "dup_group is_primary also_in review_status reviewed_by reviewed_at llm_score llm_reason "
    "match_type semantic_score topic_tag topic_standout topic_parent_id body_status exported_at"
).split()
BODY_COLUMNS = ["article_id", "content_text", "fetched_at"]


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime | date):
        return value.isoformat()
    raise TypeError(f"not JSON serializable: {type(value)!r}")


@dataclass
class ExportManifest:
    path: str
    articles_exported: int = 0
    bodies_exported: int = 0
    exported_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class ImportReport:
    articles_imported: int = 0
    articles_skipped_existing: int = 0
    bodies_imported: int = 0
    bodies_skipped_existing: int = 0


def export_to_jsonl(
    conn: DictConnection,
    path: str | Path,
    *,
    day_from: date | None = None,
    day_to: date | None = None,
    review_status: str | None = None,
    mark_exported: bool = True,
) -> ExportManifest:
    """Stream matching `articles` (+ their `article_bodies`, where
    present) into one gzip JSONL file. Filters are additive (all given
    conditions AND-ed together); no filters = export everything.

    `mark_exported=True` (the default) stamps `articles.exported_at =
    now()` on every exported row in the same transaction as the export
    scan, so `storage.purge_matched_articles(require_exported=True)`
    immediately recognizes these rows as safe to reclaim. Pass `False`
    for a read-only preview/backup that shouldn't affect purge
    eligibility.
    """
    where_clauses: list[str] = []
    params: dict[str, Any] = {}
    if day_from is not None:
        where_clauses.append("day >= %(day_from)s")
        params["day_from"] = day_from
    if day_to is not None:
        where_clauses.append("day <= %(day_to)s")
        params["day_to"] = day_to
    if review_status is not None:
        where_clauses.append("review_status = %(review_status)s")
        params["review_status"] = review_status
    where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""

    article_ids: list[int] = []
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    with gzip.open(path, "wt", encoding="utf-8") as f:
        columns_sql = ", ".join(ARTICLE_COLUMNS)
        # server-side (named) cursor: constant memory regardless of export size
        with conn.cursor(name="export_articles") as cur:
            cur.execute(f"SELECT {columns_sql} FROM articles {where_sql} ORDER BY id", params)
            articles_exported = 0
            for row in cur:
                article_ids.append(row["id"])
                f.write(json.dumps({"_table": "articles", **row}, default=_json_default))
                f.write("\n")
                articles_exported += 1

        bodies_exported = 0
        if article_ids:
            body_columns_sql = ", ".join(BODY_COLUMNS)
            with conn.cursor(name="export_bodies") as cur:
                cur.execute(
                    f"SELECT {body_columns_sql} FROM article_bodies "
                    "WHERE article_id = ANY(%(ids)s) ORDER BY article_id",
                    {"ids": article_ids},
                )
                for row in cur:
                    f.write(json.dumps({"_table": "article_bodies", **row}, default=_json_default))
                    f.write("\n")
                    bodies_exported += 1

    if mark_exported and article_ids:
        conn.execute("UPDATE articles SET exported_at = now() WHERE id = ANY(%s)", (article_ids,))

    return ExportManifest(path=str(path), articles_exported=articles_exported, bodies_exported=bodies_exported)


def _read_jsonl(path: str | Path) -> Iterator[dict[str, Any]]:
    with gzip.open(path, "rt", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                yield json.loads(line)


def import_from_jsonl(conn: DictConnection, path: str | Path, *, batch_size: int = 500) -> ImportReport:
    """Re-import a file produced by `export_to_jsonl`. Idempotent —
    `ON CONFLICT (id) DO NOTHING` — so importing the same file twice (or
    importing a file that overlaps rows already present, e.g. a re-import
    after only a partial purge) is safe and simply skips what's already
    there, counted separately as `*_skipped_existing` rather than
    silently conflated with newly imported rows.

    Same two-pass handling of `topic_parent_id` as
    `migrations/legacy/import_legacy_sqlite.py` and for the same reason:
    a self-referencing FK can point to a row later in id order than
    itself, which a single ascending pass with immediate FK checking
    cannot always satisfy.
    """
    report = ImportReport()
    insert_columns = [c for c in ARTICLE_COLUMNS if c != "topic_parent_id"]
    article_batch: list[dict[str, Any]] = []
    body_batch: list[dict[str, Any]] = []
    parent_links: list[tuple[int, int]] = []

    def flush_articles() -> None:
        nonlocal article_batch
        if not article_batch:
            return
        columns_sql = ", ".join(insert_columns)
        placeholders = ", ".join(f"%({c})s" for c in insert_columns)
        with conn.cursor() as cur:
            for row in article_batch:
                result = cur.execute(
                    f"INSERT INTO articles ({columns_sql}) VALUES ({placeholders}) "
                    "ON CONFLICT (id) DO NOTHING RETURNING id",
                    row,
                ).fetchone()
                if result is not None:
                    report.articles_imported += 1
                else:
                    report.articles_skipped_existing += 1
        article_batch = []

    def flush_bodies() -> None:
        nonlocal body_batch
        if not body_batch:
            return
        with conn.cursor() as cur:
            for row in body_batch:
                result = cur.execute(
                    "INSERT INTO article_bodies (article_id, content_text, fetched_at) "
                    "VALUES (%(article_id)s, %(content_text)s, %(fetched_at)s) "
                    "ON CONFLICT (article_id) DO NOTHING RETURNING article_id",
                    row,
                ).fetchone()
                if result is not None:
                    report.bodies_imported += 1
                else:
                    report.bodies_skipped_existing += 1
        body_batch = []

    for obj in _read_jsonl(path):
        table = obj.pop("_table", None)
        if table == "articles":
            topic_parent_id = obj.pop("topic_parent_id", None)
            article_batch.append(obj)
            if topic_parent_id is not None:
                parent_links.append((topic_parent_id, obj["id"]))
            if len(article_batch) >= batch_size:
                flush_articles()
        elif table == "article_bodies":
            body_batch.append(obj)
            if len(body_batch) >= batch_size:
                flush_bodies()
        # unrecognized _table values are ignored rather than raising —
        # forward-compatible with a future export format adding a table
        # this version of import doesn't know about yet.

    flush_articles()
    flush_bodies()

    if parent_links:
        with conn.cursor() as cur:
            cur.executemany(
                "UPDATE articles SET topic_parent_id = %s WHERE id = %s AND topic_parent_id IS NULL",
                parent_links,
            )

    if report.articles_imported:
        conn.execute(
            "SELECT setval(pg_catalog.pg_get_serial_sequence('articles', 'id'), "
            "COALESCE((SELECT max(id) FROM articles), 1))"
        )

    return report
