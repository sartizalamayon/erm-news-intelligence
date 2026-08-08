"""Ad-hoc Excel/CSV export builder (frontend-contract.md §3.4): story-set
selection (Top N / Approved / Matched / Starred), date-range scope
(today/yesterday/week/month/custom range), section filtering, column
selection AND ordering, sort, a live row-count preview, and named
presets.

Does NOT reimplement app.services.data_transfer's JSONL export/import —
that module is the admin storage-lifecycle "export prior data, purge,
re-import" journey (ADR §9.2), a completely different feature from this
one (an analyst's ad-hoc spreadsheet pull). This module reads `articles`
directly with its own filter set.

Both writers are real: `export_csv` uses the stdlib `csv` module;
`export_xlsx` uses `openpyxl` (added to pyproject.toml by ARCH-REPO,
commit 9ba3ad9, after this module originally had to stub the xlsx path
at 501 for its absence).

Column selection: `COLUMN_KEYS` mirrors frontend/js/screens/export.js's
`COLUMNS` array verbatim (same keys, same order) — a caller passes an
ordered `columns` list and gets exactly those columns, in that order,
in either writer. `DEFAULT_COLUMNS` matches that screen's `s.cols`
default-on set for when no `columns` is given (export.js's `params()`
did not send the user's selection at all before FE-WIRE wired it
through to these two writers).

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

from openpyxl import Workbook

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

# ── columns (frontend-contract.md §3.4's exColumns[], export.js's COLUMNS) ──

COLUMN_KEYS: tuple[str, ...] = (
    "headline", "source", "section", "keywords", "published", "score", "reason", "alsoIn", "link", "body",
)
# export.js's `s.cols` default-on set, in that object's own key order
# (reason and body default OFF).
DEFAULT_COLUMNS: tuple[str, ...] = (
    "headline", "source", "section", "keywords", "published", "score", "alsoIn", "link",
)
COLUMN_HEADERS: dict[str, str] = {
    "headline": "Headline",
    "source": "Source",
    "section": "Section",
    "keywords": "Keywords hit",
    "published": "Published",
    "score": "Relevance",
    "reason": "Reason",
    "alsoIn": "Also reported by",
    "link": "Link",
    "body": "Full article text",
}


def _today() -> date:
    return datetime.now(DHAKA).date()


def _tier_label(score: float | None) -> str:
    if score is None:
        return ""
    if score >= 8:
        return "High"
    if score >= 6:
        return "Medium"
    if score >= 5:
        return "Low"
    return "Least"


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


def resolve_columns(columns: list[str] | None) -> list[str]:
    """Selection AND ordering: whatever order the caller passes is the
    output column order, matching export.js's own "in this order" copy.
    Unknown keys are dropped rather than raising — forward-compatible
    with a caller sending a stale key.
    """
    if not columns:
        return list(DEFAULT_COLUMNS)
    resolved = [c for c in columns if c in COLUMN_KEYS]
    return resolved or list(DEFAULT_COLUMNS)


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


def counts_for_scope(
    conn: DictConnection, *, day_from: date, day_to: date, sections: list[str], top_n: int
) -> dict[str, int]:
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


def _published_str(row: dict[str, Any]) -> str:
    published = row.get("published")
    if published:
        return published.astimezone(DHAKA).strftime("%Y-%m-%d %H:%M")
    return str(row.get("day") or "")


def _column_value(key: str, row: dict[str, Any]) -> Any:
    if key == "headline":
        title = row["title"]
        title_en = row.get("title_en") or ""
        return f"{title}\n{title_en}" if title_en and title_en != title else title
    if key == "source":
        return row["source"]
    if key == "section":
        sections = row.get("sections") or ""
        return sections.split(", ")[0] if sections else ""
    if key == "keywords":
        return row.get("keywords") or ""
    if key == "published":
        return _published_str(row)
    if key == "score":
        score = row.get("llm_score")
        if score is None:
            return ""
        return f"{score}/10 ({_tier_label(score)})"
    if key == "reason":
        return row.get("llm_reason") or ""
    if key == "alsoIn":
        return row.get("also_in") or ""
    if key == "link":
        return row.get("url") or ""
    if key == "body":
        return row.get("content_text") or ""
    return ""


def _fetch_export_rows(
    conn: DictConnection, *, filter: str, top_n: int, scope: str, date_from: str | None, date_to: str | None,
    sections: list[str], sort: str,
) -> tuple[list[dict[str, Any]], date, date]:
    day_from, day_to, _ = resolve_scope(scope, date_from, date_to)
    rows = fetch_pool(
        conn, filter_key=filter, top_n=top_n, day_from=day_from, day_to=day_to, sections=sections, sort=sort
    )
    return rows, day_from, day_to


def export_csv(
    conn: DictConnection, *, filter: str, top_n: int, scope: str, date_from: str | None, date_to: str | None,
    sections: list[str], sort: str, columns: list[str] | None = None,
) -> tuple[bytes, str]:
    rows, _day_from, _day_to = _fetch_export_rows(
        conn, filter=filter, top_n=top_n, scope=scope, date_from=date_from, date_to=date_to,
        sections=sections, sort=sort,
    )
    cols = resolve_columns(columns)

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([COLUMN_HEADERS[c] for c in cols])
    for r in rows:
        writer.writerow([_column_value(c, r) for c in cols])
    filename = _filename(filter, top_n, scope, "csv")
    return buf.getvalue().encode("utf-8-sig"), filename


def export_xlsx(
    conn: DictConnection, *, filter: str, top_n: int, scope: str, date_from: str | None, date_to: str | None,
    sections: list[str], sort: str, columns: list[str] | None = None,
) -> tuple[bytes, str]:
    rows, _day_from, _day_to = _fetch_export_rows(
        conn, filter=filter, top_n=top_n, scope=scope, date_from=date_from, date_to=date_to,
        sections=sections, sort=sort,
    )
    cols = resolve_columns(columns)

    wb = Workbook()
    ws = wb.active
    ws.title = "Export"
    ws.append([COLUMN_HEADERS[c] for c in cols])
    for r in rows:
        ws.append([_column_value(c, r) for c in cols])
    for i, _c in enumerate(cols, start=1):
        ws.column_dimensions[ws.cell(row=1, column=i).column_letter].width = 22

    buf = io.BytesIO()
    wb.save(buf)
    filename = _filename(filter, top_n, scope, "xlsx")
    return buf.getvalue(), filename


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
    sections: list[str], sort: str, format: str, columns: list[str] | None = None,
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
        "columns": resolve_columns(columns),
    }
    set_setting(conn, f"export_preset::{preset_id}", json.dumps(data))
    return data
