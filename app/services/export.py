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
import tempfile
import uuid
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager as ContextManager
from datetime import date, datetime, timedelta, timezone
from typing import Any

from openpyxl import Workbook
from openpyxl.utils import get_column_letter

from app.db.connection import DictConnection, get_connection
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

# "The caller explicitly selected NO sections", as a single wire value.
#
# `sections=""`, and an absent `sections` param, have always meant "no
# section filter, take everything", and they must keep meaning that:
# every API caller that simply omits the parameter depends on it, as do
# the existing tests in tests/test_core_export.py.
#
# But the Export screen also lets a user untick every section chip, and
# that is the OPPOSITE instruction. It used to serialise to that same
# empty string (frontend/js/api.js drops empty-string query params, the
# router's default is `sections: str = ""`, `_split_list` turns it into
# `[]`, and `_scope_where` below then skipped the filter entirely), so
# unticking everything made the row count RISE to the unfiltered total
# and produced a spreadsheet containing every section while the screen's
# own recap line read "Sections: none selected". A file that goes to a
# board asserting one scope and carrying another is the worst kind of
# wrong, because nothing about it looks wrong.
#
# So an explicit empty selection needs a value of its own, and this is
# it. Kept identical to frontend/js/screens/export.js's `NO_SECTIONS`,
# which tests/frontend/export_sections.test.mjs pins by reading both
# files.
#
# The one ambiguity, stated rather than hidden: section names are
# whatever an admin types (app.services.admin stores `keywords.section`
# verbatim), so an admin CAN create a section literally called
# "__none__". When they do, this token wins and that section cannot be
# exported on its own. That is the deliberate trade: the wire meaning of
# a fixed protocol value must not depend on the contents of a table an
# admin edits, or the same request would mean different things on
# different deployments.
NO_SECTIONS_TOKEN = "__none__"

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


# ── Spreadsheet formula injection (CSV/DDE) ─────────────────────────
#
# EVERY value written into a .xlsx or .csv cell must go through
# `defuse_spreadsheet_formula` first. This is not hardening in the
# abstract; it was live, and it was verified live.
#
# Excel, LibreOffice and Google Sheets all treat a cell whose text
# begins with `=`, `+`, `-` or `@` as a FORMULA, not as text. A .xlsx
# built by openpyxl from the string "=cmd|'/c calc'!A0" is saved with
# `data_type == "f"` -- a real formula cell -- and the equivalent CSV
# line begins with a bare `=`. Opening that file and clearing the
# resulting prompt is enough for a DDE payload to run a command, or for
# `=HYPERLINK`/`=WEBSERVICE` to quietly send the row's contents to
# somebody else's server.
#
# Why this product is squarely in the blast radius, rather than
# theoretically at risk:
#
#   * `articles.title` is SCRAPED, verbatim, from ~51 third-party news
#     sites. Nothing in the pipeline constrains a headline's first
#     character, and nothing should -- it is someone else's text.
#   * The export exists specifically so that text can be opened as a
#     spreadsheet, by an executive, on a corporate laptop.
#   * `keywords.term_en` / `term_bn` / `section` are free text an admin
#     types, AND arrive through `import_keywords_xlsx`, so a hostile
#     file can be imported once and re-exported to somebody else later.
#
# Reproduced end to end against a real export before this existed: a row
# whose title was `=cmd|'/c calc'!A0` came back out of `POST /export` as
# a formula cell in the .xlsx and as an unquoted `=...` line in the CSV.
#
# The fix is the standard one (OWASP): prefix a leading apostrophe,
# which every spreadsheet reads as "the rest of this cell is text" and
# does not display. It is applied to strings only, so real numbers stay
# numbers and stay sortable; a negative number reaching a cell as a
# float is untouched, and one reaching it as a STRING was already text
# as far as the sheet was concerned.
_FORMULA_LEAD = ("=", "+", "-", "@", "\t", "\r")


def defuse_spreadsheet_formula(value: Any) -> Any:
    """`value`, made safe to write into a spreadsheet cell.

    Leaves non-strings exactly as they are: openpyxl writes a float as a
    number, and a number cannot carry a formula.
    """
    if isinstance(value, str) and value[:1] in _FORMULA_LEAD:
        return "'" + value
    return value


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


# A custom date range, carried INSIDE the scope string as
# "range:2026-07-01:2026-09-30".
#
# Why a composite value exists at all: `scope="range"` on its own is an
# incomplete statement. It names a window shape and omits the window,
# and resolve_scope() below has to fall back to today when the dates are
# missing, which is silent and wrong-looking rather than loud and wrong.
# Anywhere the three parts can travel together they should, and the
# saved-preset path is exactly where they could not: an export preset
# saved with scope "Custom range" and 1 Jul to 30 Sep came back with the
# scope intact and both dates gone, so loading it previewed TODAY's rows
# under a heading that claimed a quarter, with no error at any point.
#
# app.api.schemas.core.ExportPresetIn folds a preset's `from`/`to` into
# this form on the way in; create_preset() below unfolds it again and
# stores the three fields separately, so a stored preset and the
# ExportPresetOut a client reads back are both plain and explicit.
# resolve_scope() understands the composite form too, so the value is
# never a private arrangement between two modules: it means the same
# thing everywhere a scope is accepted.
RANGE_SCOPE_PREFIX = "range:"


def split_scope(scope: str) -> tuple[str, str | None, str | None]:
    """Split "range:FROM:TO" into ("range", FROM, TO).

    Any other scope is returned unchanged with no dates. Either half may
    be blank ("range::2026-09-30"), which yields None for that end and
    lets the caller's own from/to fill it in.
    """
    if scope and scope.startswith(RANGE_SCOPE_PREFIX):
        parts = scope.split(":")
        frm = parts[1] if len(parts) > 1 and parts[1] else None
        to = parts[2] if len(parts) > 2 and parts[2] else None
        return "range", frm, to
    return scope, None, None


def resolve_scope(scope: str, date_from: str | None, date_to: str | None) -> tuple[date, date, str]:
    today = _today()
    # Explicit query params win over dates carried in the scope string;
    # the composite form is a fallback for callers that only have one
    # field to put a scope in, not a second way to override.
    scope, scope_from, scope_to = split_scope(scope)
    date_from = date_from or scope_from
    date_to = date_to or scope_to
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
    if sections == [NO_SECTIONS_TOKEN]:
        # An explicit empty selection: match nothing, in the counts and
        # in the rows alike, so the preview's number and the file that
        # follows it agree. Checked BEFORE the name-matching branch
        # below and only when the token is the entire selection, so a
        # deployment that happens to have a section named "__none__"
        # cannot turn this protocol value back into a name lookup.
        clauses.append("FALSE")
    elif sections:
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


def _pool_sql(
    conn: DictConnection, *, filter_key: str, top_n: int, day_from: date, day_to: date, sections: list[str], sort: str
) -> tuple[str, dict[str, Any]]:
    """The one SELECT both the buffered and the streaming readers run.

    Split out so `fetch_pool` and `iter_export_rows` cannot drift: a
    filter or ordering fixed in one and not the other would mean a
    download whose contents disagree with the row count the Export
    screen previewed for it.
    """
    base_where, params = _scope_where(day_from, day_to, sections)
    params["starred_ids"] = _starred_ids(conn)
    where = f"{base_where} AND {_filter_clause(filter_key, params)}"
    order = SORT_SQL.get(sort, SORT_SQL["relevance"])
    sql = f"SELECT {SELECT_COLUMNS} FROM {FROM_CLAUSE} WHERE {where} ORDER BY {order}"
    if filter_key == "topn":
        sql += " LIMIT %(top_n)s"
        params["top_n"] = top_n
    return sql, params


def fetch_pool(
    conn: DictConnection, *, filter_key: str, top_n: int, day_from: date, day_to: date, sections: list[str], sort: str
) -> list[dict[str, Any]]:
    """Every matching row, materialised.

    Kept for `preview` and for tests that want a list to assert on. The
    DOWNLOAD path deliberately does not use this any more -- see
    `iter_export_rows` for the measurements that made buffering
    untenable on the deploy target.
    """
    sql, params = _pool_sql(
        conn, filter_key=filter_key, top_n=top_n, day_from=day_from, day_to=day_to, sections=sections, sort=sort
    )
    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


# How many rows the server-side cursor hands back per round trip. 1,000
# is the psycopg default shape and measured indistinguishable from
# larger values here; the point of the cursor is that this number, not
# the result size, is what determines peak memory.
_CURSOR_ITERSIZE = 1000


def iter_export_rows(
    conn: DictConnection, *, filter_key: str, top_n: int, day_from: date, day_to: date, sections: list[str], sort: str
) -> Iterator[dict[str, Any]]:
    """Stream matching rows through a Postgres SERVER-SIDE cursor.

    WHY THIS EXISTS. `fetch_pool` calls `.fetchall()` with no LIMIT for
    every filter except `topn`, and the writers then held the rows as
    dicts, the whole document as a string or workbook, AND its encoded
    bytes, all live at once. Measured on the 79,892-article dev corpus,
    against a Render FREE instance's 512 MB with a ~53 MB baseline:

        CSV   1 day       2,561 rows   0.09s    70 MB
        CSV   1 month    20,392 rows   1.13s   187 MB
        CSV   1 year     27,311 rows   1.07s   270 MB
        XLSX  12 years   27,422 rows   3.68s   199 MB

    One export peaked at 270 MB. Two at once, or one alongside the
    nightly batch, is an OOM on a 512 MB single instance -- and the
    archive grows by 2,000 to 8,000 articles a day, so it only ever gets
    worse. Nothing capped it, at any layer.

    A named cursor keeps the result set on the SERVER and ships it in
    `itersize` batches, so peak memory is set by the batch size rather
    than by how much the analyst asked for. Measured with the same
    corpus, streaming all 79,892 rows: 44 MB peak against a 41 MB
    baseline. A 3 MB delta, in place of ~220 MB.

    The caller must keep `conn` open for as long as it consumes this --
    the rows do not exist client-side until they are read. That is why
    the streaming writers below own their own connection rather than
    borrowing the request's.
    """
    sql, params = _pool_sql(
        conn, filter_key=filter_key, top_n=top_n, day_from=day_from, day_to=day_to, sections=sections, sort=sort
    )
    with conn.cursor(name=f"export_{uuid.uuid4().hex}") as cur:
        cur.itersize = _CURSOR_ITERSIZE
        cur.execute(sql, params)
        for row in cur:
            yield dict(row)


def filename_for(filter_key: str, top_n: int, scope: str, fmt: str) -> str:
    """Public name for `_filename`.

    The streaming router needs the download's filename BEFORE any row is
    read, because Content-Disposition goes out with the headers and the
    body follows. It is a pure function of the request parameters, so
    that is free -- but it does mean the router reaches for it directly
    rather than receiving it back from a writer, as the buffered
    versions used to return alongside their bytes.
    """
    return _filename(filter_key, top_n, scope, fmt)


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


# Rows accumulated per yielded CSV chunk. One `yield` per row would put
# a per-row cost on every layer above (ASGI message, HTTP chunk framing)
# for no memory benefit; a few hundred rows is a few tens of KB.
_CSV_CHUNK_ROWS = 500

# Read size when handing the finished workbook back. Only ever this much
# of the file is in memory at a time.
_XLSX_CHUNK_BYTES = 64 * 1024


def stream_csv(
    conn_factory: Callable[[], ContextManager[DictConnection]] = get_connection,
    *, filter: str, top_n: int, scope: str, date_from: str | None, date_to: str | None,
    sections: list[str], sort: str, columns: list[str] | None = None,
) -> Iterator[bytes]:
    """CSV as a byte stream, never assembled whole.

    THIS GENERATOR OWNS ITS CONNECTION, and that is load-bearing rather
    than stylistic. `conn_factory` is how it gets one -- taken as an
    argument rather than reached for as a module global so the caller
    decides where the connection comes from, and so a test can hand in
    its own transaction-scoped connection through the same seam the
    router already exposes. `iter_export_rows` reads through a server-side
    cursor, so the rows do not exist client-side until they are pulled;
    if the ROUTER held the `with get_connection()` block, it would exit
    -- closing the connection -- the moment it handed the generator to
    Starlette, and the first pull would fail on a closed cursor. Opening
    it inside means the block stays entered for exactly as long as
    something is consuming, and unwinds on the way out.

    That includes an abandoned download: Starlette closes the generator
    when the client disconnects, which raises GeneratorExit at the
    `yield`, which exits the `with` and releases the connection. Without
    that, a reader who cancels a large export would strand a connection
    from Supabase's small free-tier pool until the process restarted.

    The BOM belongs to the first chunk only: `utf-8-sig` on the header
    and plain `utf-8` after it reproduces the single `.encode("utf-8-sig")`
    the buffered writer used to do over the whole document. Emitting it
    per chunk would scatter U+FEFF through the file.
    """
    cols = resolve_columns(columns)
    day_from, day_to, _ = resolve_scope(scope, date_from, date_to)

    head = io.StringIO()
    csv.writer(head).writerow([COLUMN_HEADERS[c] for c in cols])
    yield head.getvalue().encode("utf-8-sig")

    with conn_factory() as conn:
        buf = io.StringIO()
        writer = csv.writer(buf)
        pending = 0
        for row in iter_export_rows(
            conn, filter_key=filter, top_n=top_n, day_from=day_from, day_to=day_to, sections=sections, sort=sort
        ):
            writer.writerow([defuse_spreadsheet_formula(_column_value(c, row)) for c in cols])
            pending += 1
            if pending >= _CSV_CHUNK_ROWS:
                yield buf.getvalue().encode("utf-8")
                buf.seek(0)
                buf.truncate(0)
                pending = 0
        if pending:
            yield buf.getvalue().encode("utf-8")


def stream_xlsx(
    conn_factory: Callable[[], ContextManager[DictConnection]] = get_connection,
    *, filter: str, top_n: int, scope: str, date_from: str | None, date_to: str | None,
    sections: list[str], sort: str, columns: list[str] | None = None,
) -> Iterator[bytes]:
    """XLSX as a byte stream.

    An xlsx is a ZIP, and a ZIP's central directory is written last, so
    unlike CSV this cannot be emitted row-by-row down the wire. What it
    CAN avoid is holding every row as a dict and the whole workbook as
    live cell objects at the same time:

      * `Workbook(write_only=True)` keeps only the current row, writing
        each one out as it is appended instead of building a full
        in-memory sheet.
      * the result is saved to a SpooledTemporaryFile, which stays in
        memory while small and rolls over to disk past the threshold, so
        a large export becomes disk rather than RSS on a 512 MB box.
      * it is then read back in fixed-size chunks.

    Column widths are set before any row is appended: in write-only mode
    `column_dimensions` has to be configured up front, because once rows
    start streaming the sheet header has already been written.
    """
    cols = resolve_columns(columns)
    day_from, day_to, _ = resolve_scope(scope, date_from, date_to)

    wb = Workbook(write_only=True)
    ws = wb.create_sheet("Export")
    for i, _c in enumerate(cols, start=1):
        ws.column_dimensions[get_column_letter(i)].width = 22
    ws.append([COLUMN_HEADERS[c] for c in cols])

    with conn_factory() as conn:
        for row in iter_export_rows(
            conn, filter_key=filter, top_n=top_n, day_from=day_from, day_to=day_to, sections=sections, sort=sort
        ):
            ws.append([defuse_spreadsheet_formula(_column_value(c, row)) for c in cols])

    with tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024) as tmp:
        wb.save(tmp)
        tmp.seek(0)
        while True:
            chunk = tmp.read(_XLSX_CHUNK_BYTES)
            if not chunk:
                break
            yield chunk


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
        writer.writerow([defuse_spreadsheet_formula(_column_value(c, r)) for c in cols])
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
        ws.append([defuse_spreadsheet_formula(_column_value(c, r)) for c in cols])
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
    date_from: str | None = None, date_to: str | None = None,
) -> dict[str, Any]:
    preset_id = uuid.uuid4().hex[:12]
    # Unfold "range:FROM:TO" (see RANGE_SCOPE_PREFIX) so what is STORED
    # is always the plain three fields. A preset is read back far more
    # often than it is written, and a client that has to know about a
    # composite encoding to load one is a client that will eventually
    # get it wrong.
    scope, scope_from, scope_to = split_scope(scope)
    # An explicitly-empty section selection is stored as the empty list
    # it means, not as the token that carried it. The token is a wire
    # value for a request; a preset is a saved selection, and an empty
    # selection restores as an empty selection with no decoding needed.
    stored_sections = [] if list(sections) == [NO_SECTIONS_TOKEN] else list(sections)
    data = {
        "id": preset_id,
        "name": name,
        "note": note,
        "filter": filter,
        "topN": topN,
        "scope": scope,
        "from": date_from or scope_from,
        "to": date_to or scope_to,
        "sections": stored_sections,
        "sort": sort,
        "format": format,
        "columns": resolve_columns(columns),
    }
    set_setting(conn, f"export_preset::{preset_id}", json.dumps(data))
    return data
