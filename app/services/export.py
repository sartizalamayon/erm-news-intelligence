"""Ad-hoc Excel/CSV export builder (frontend-contract.md §3.4): story-set
selection (Top N / Approved / Matched / Starred), date-range scope
(today/yesterday/week/month/custom range), section filtering, sort, a
live row-count preview, and named presets.

Does NOT reimplement app.services.data_transfer's JSONL export/import —
that module is the admin storage-lifecycle "export prior data, purge,
re-import" journey (ADR §9.2), a completely different feature from this
one (an analyst's ad-hoc spreadsheet pull). This module reads `articles`
directly with its own filter set.

xlsx writing needs `openpyxl`, which is NOT a project dependency
(pyproject.toml's "only ARCH-REPO edits the manifest" convention, and
this task was told to flag rather than add). CSV needs nothing beyond
the stdlib `csv` module, so `export_csv` below is real and working; the
router's xlsx branch returns a clear 501 instead of a fake file — see
app/api/routers/export.py.

Presets are persisted in the generic `settings` key/value table
(0001_settings.sql) under `export_preset::<uuid>` keys, one row per
preset — same reuse-not-reinvent move as app/api/routers/review.py's
star/note storage (see that module's docstring) and ARCH-DB's own
purge-preview snapshot (storage-policy.md §4.1). Flagged back to
ARCH-DB/the product owner: a dedicated `export_presets` table would be
the cleaner long-term home, and open question #12 (frontend-contract.md
§8: presets per-user or org-wide?) is unresolved — presets are
org-wide/shared here, the simpler of the two readings, until that's
decided.
"""

from __future__ import annotations

import csv
import io
import json
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any

from app.db.connection import DictConnection
from app.db.settings import set_setting

DHAKA = timezone(timedelta(hours=6))

FILTER_KEYS = ("topn", "approved", "matched", "starred")

SORT_SQL = {
    "relevance": "a.llm_score DESC NULLS LAST, a.id DESC",
    "section": "a.sections ASC, a.llm_score DESC NULLS LAST",
    "source": "a.source ASC, a.llm_score DESC NULLS LAST",
    "date": "COALESCE(a.published, a.fetched_at) DESC",
}

SCOPE_LABELS = {
    "today": "today",
    "yesterday": "yesterday",
    "week": "the last 7 days",
    "month": "this month",
}

SELECT_COLUMNS = (
    "a.id, a.url, a.source, a.title, a.title_en, a.published, a.day, a.sections, a.keywords, "
    "a.llm_score, a.llm_reason, a.also_in, a.review_status, COALESCE(ab.content_text, '') AS content_text"
)
FROM_CLAUSE = "articles a LEFT JOIN article_bodies ab ON ab.article_id = a.id"


def _today() -> date:
    return datetime.now(DHAKA).date()


def resolve_scope(scope: str, date_from: str | None, date_to: str | None) -> tuple[date, date, str]:
    today = _today()
    if scope == "yesterday":
        d = today - timedelta(days=1)
        return d, d, SCOPE_LABELS["yesterday"]
    if scope == "week":
        return today - timedelta(days=6), today, SCOPE_LABELS["week"]
    if scope == "month":
        return today.replace(day=1), today, SCOPE_LABELS["month"]
    if scope == "range":
        try:
            frm = date.fromisoformat(date_from) if date_from else today
            to = date.fromisoformat(date_to) if date_to else today
        except ValueError:
            frm, to = today, today
        return frm, to, f"{frm.isoformat()} to {to.isoformat()}"
    return today, today, SCOPE_LABELS["today"]


def _starred_ids(conn: DictConnection) -> list[int]:
    rows = conn.execute("SELECT key FROM settings WHERE key LIKE 'article_star::%' AND value = 'true'").fetchall()
    out = []
    for row in rows:
        try:
            out.append(int(row["key"].split("::", 1)[1]))
        except ValueError:
            continue
    return out


def _scope_where(day_from: date, day_to: date, sections: list[str]) -> tuple[str, dict[str, Any]]:
    clauses = ["a.day >= %(day_from)s", "a.day <= %(day_to)s", "COALESCE(a.is_primary, TRUE) = TRUE"]
    params: dict[str, Any] = {"day_from": day_from, "day_to": day_to}
    if sections:
        clauses.append(
            "EXISTS (SELECT 1 FROM unnest(string_to_array(a.sections, ', ')) sec WHERE sec = ANY(%(sections)s))"
        )
        params["sections"] = list(sections)
    return " AND ".join(clauses), params


def _filter_clause(filter_key: str, params: dict[str, Any]) -> str:
    if filter_key == "approved":
        return "a.review_status = 'approved'"
    if filter_key == "starred":
        return "a.id = ANY(%(starred_ids)s)"
    # "matched" and "topn" both draw from the full matched pool
    return "a.review_status != 'filtered_out'"


def counts_for_scope(conn: DictConnection, *, day_from: date, day_to: date, sections: list[str], top_n: int) -> dict[str, int]:
    base_where, params = _scope_where(day_from, day_to, sections)
    params["starred_ids"] = _starred_ids(conn)
    out: dict[str, int] = {}
    for key in ("approved", "matched", "starred"):
        where = f"{base_where} AND {_filter_clause(key, params)}"
        row = conn.execute(f"SELECT count(*) AS n FROM {FROM_CLAUSE} WHERE {where}", params).fetchone()
        out[key] = int(row["n"]) if row else 0
    out["topn"] = min(top_n, out["matched"])
    return out


def fetch_pool(
    conn: DictConnection, *, filter_key: str, top_n: int, day_from: date, day_to: date, sections: list[str], sort: str
) -> list[dict[str, Any]]:
    base_where, params = _scope_where(day_from, day_to, sections)
    params["starred_ids"] = _starred_ids(conn)
    where = f"{base_where} AND {_filter_clause(filter_key, params)}"
    order = SORT_SQL.get(sort, SORT_SQL["relevance"])
    sql = f"SELECT {SELECT_COLUMNS} FROM {FROM_CLAUSE} WHERE {where} ORDER BY {order}"
    if filter_key == "topn":
        sql += " LIMIT %(top_n)s"
        params["top_n"] = top_n
    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def _filename(filter_key: str, top_n: int, scope: str, fmt: str) -> str:
    today = _today()
    label = f"{filter_key}{top_n}" if filter_key == "topn" else filter_key
    return f"erm_{label}_{scope}_{today.isoformat()}.{fmt}"


def preview(
    conn: DictConnection, *, filter: str, top_n: int, scope: str, date_from: str | None, date_to: str | None,
    sections: list[str], sort: str, format: str,
) -> dict[str, Any]:
    day_from, day_to, scope_label = resolve_scope(scope, date_from, date_to)
    counts = counts_for_scope(conn, day_from=day_from, day_to=day_to, sections=sections, top_n=top_n)
    rows = counts.get(filter, 0)

    if filter == "topn":
        summary = f"The {rows} highest-scoring matched articles from {scope_label}."
    elif filter == "approved":
        summary = f"The {rows} articles approved from {scope_label}."
    elif filter == "starred":
        summary = f"The {rows} starred articles from {scope_label}."
    else:
        summary = f"All {rows} keyword-matched articles from {scope_label}."

    return {
        "rows": rows,
        "summary": summary,
        "filename": _filename(filter, top_n, scope, format),
        "counts": counts,
    }


CSV_HEADER = ["Headline", "Headline (EN)", "Source", "Section", "Keywords hit", "Published", "Relevance", "Also reported by", "Link"]


def export_csv(
    conn: DictConnection, *, filter: str, top_n: int, scope: str, date_from: str | None, date_to: str | None,
    sections: list[str], sort: str,
) -> tuple[bytes, str]:
    """Builds a CSV using the same default-on column set
    frontend/js/screens/export.js's `s.cols` initializes to
    (headline/source/section/keywords/published/score/alsoIn/link) —
    export.js's `params()` does not actually send the user's column
    selection to the API yet (a gap in that screen, not this module; see
    the mission report), so there is nothing to parameterize columns
    from today.
    """
    day_from, day_to, _ = resolve_scope(scope, date_from, date_to)
    rows = fetch_pool(conn, filter_key=filter, top_n=top_n, day_from=day_from, day_to=day_to, sections=sections, sort=sort)

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(CSV_HEADER)
    for r in rows:
        published = r["published"].astimezone(DHAKA).strftime("%Y-%m-%d %H:%M") if r.get("published") else str(r.get("day") or "")
        writer.writerow(
            [
                r["title"],
                r.get("title_en") or "",
                r["source"],
                (r.get("sections") or "").split(", ")[0] if r.get("sections") else "",
                r.get("keywords") or "",
                published,
                r.get("llm_score") if r.get("llm_score") is not None else "",
                r.get("also_in") or "",
                r.get("url") or "",
            ]
        )
    filename = _filename(filter, top_n, scope, "csv")
    return buf.getvalue().encode("utf-8-sig"), filename


# ── presets ──────────────────────────────────────────────────────────


def list_presets(conn: DictConnection) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT key, value FROM settings WHERE key LIKE 'export_preset::%' ORDER BY key").fetchall()
    out = []
    for row in rows:
        try:
            out.append(json.loads(row["value"]))
        except json.JSONDecodeError:
            continue
    return out


def create_preset(
    conn: DictConnection, *, name: str, note: str, filter: str, topN: int, scope: str,
    sections: list[str], sort: str, format: str,
) -> dict[str, Any]:
    preset_id = uuid.uuid4().hex[:12]
    data = {
        "id": preset_id,
        "name": name,
        "note": note,
        "filter": filter,
        "topN": topN,
        "scope": scope,
        "sections": sections,
        "sort": sort,
        "format": format,
    }
    set_setting(conn, f"export_preset::{preset_id}", json.dumps(data))
    return data
