"""Request/response models for the core (non-admin) API: Dashboard
analytics, Review queue, Export, Daily Risk Insight brief, Search
archive. See frontend-contract.md §3.1-§3.6 and §9 for the shapes these
mirror; each screen's `frontend/js/screens/*.js` header comment documents
the exact JSON this module was built against.

Distinct from app.api.schemas.admin (ADMIN-API's models for Sources/
Keywords/Prompts/Source health/Storage) — this module is the CORE-API's
own screens, gated by `get_current_user`, not `require_admin`.

A few plain module-level constants live here too (RISK_SECTIONS, the
score-tier SQL bands) rather than in a dedicated shared module, since
every core router already imports from this file and GATE 0 rules out
ceremony for something this small — see each constant's own docstring.
"""

from __future__ import annotations

import time

from pydantic import BaseModel, Field, model_validator

from app.db.connection import DictConnection

# ── Shared constants (not pydantic — reused across dashboard/review/export/brief) ──

# HISTORICAL NOTE / hardcoded-sections bug fix: this used to be a static
# 5-value tuple ("Geopolitical", "Macroeconomy", "Telco/regulatory",
# "Political", "Others") duplicated here, in app/services/brief.py, and
# in frontend/js/util.js. KEYWORDS-CONFIG (a concurrent task) made
# `keywords.section` admin-creatable — an admin can add e.g. a
# "Competitors" section with no migration or code change — but a static
# list meant that new section would never show up anywhere still reading
# it. `get_risk_sections()` below replaces the static tuple with a real,
# cached query over distinct `keywords.section` values so the dashboard
# picks up new sections automatically. `app/services/brief.py`'s own
# RISK_SECTIONS constant is a deliberately separate, untouched duplicate
# — the Daily Risk Insight screen is being discarded this round (see
# this task's report), not fixed.
#
# KNOWN DEFECT, deliberately still here, so the next person does not
# have to rediscover it: on a FRESH install the `keywords` table is
# empty (no migration seeds it), so get_risk_sections() below falls back
# to these five names and GET /dashboard/today's "By section" card
# renders five sections that nobody configured, each at 0. The honest
# answer there is an empty list, meaning "no sections configured yet" —
# the same answer frontend/js/util.js now gives, where the matching
# five-name fallback WAS removed this round.
#
# The one-line fix is to drop the `or _RISK_SECTIONS_FALLBACK` from the
# success branch of get_risk_sections() (keeping it on the except
# branch, where "the query failed" genuinely is a different thing from
# "there are none"). It was not applied here because it fails
# tests/test_core_dashboard.py::test_today_counts_reflect_real_rows,
# which asserts `section_labels == {those five}` against an empty
# keywords table, and that file is outside this change's ownership. Both
# edits have to land together.
_RISK_SECTIONS_FALLBACK: tuple[str, ...] = ("Geopolitical", "Macroeconomy", "Telco/regulatory", "Political", "Others")
_RISK_SECTIONS_CACHE_TTL_SECONDS = 60.0
_risk_sections_cache: tuple[str, ...] | None = None
_risk_sections_cache_at = 0.0


def get_risk_sections(conn: DictConnection) -> tuple[str, ...]:
    """Distinct keyword sections currently configured (NULL sections
    normalized to "Uncategorized"), derived via
    app.services.admin.list_keyword_sections — one source of truth for
    what "a section" means, shared with the Keywords admin screen —
    rather than a second, possibly-diverging query here. Falls back to
    the original fixed 5-value list when no keywords exist yet (a fresh
    install) or the query fails for any reason, so callers never see an
    empty section list. Cached for `_RISK_SECTIONS_CACHE_TTL_SECONDS`
    (this runs on every `/dashboard/today` request; section rows change
    rarely) rather than queried fresh every time.
    """
    global _risk_sections_cache, _risk_sections_cache_at
    now = time.monotonic()
    if _risk_sections_cache is not None and (now - _risk_sections_cache_at) < _RISK_SECTIONS_CACHE_TTL_SECONDS:
        return _risk_sections_cache

    from app.services.admin import list_keyword_sections  # lazy: keep schemas -> services out of module load order

    try:
        rows = list_keyword_sections(conn)
        sections = tuple(r["section"] for r in rows if r.get("section")) or _RISK_SECTIONS_FALLBACK
    except Exception:
        sections = _risk_sections_cache or _RISK_SECTIONS_FALLBACK

    _risk_sections_cache = sections
    _risk_sections_cache_at = now
    return sections


def _reset_risk_sections_cache_for_tests() -> None:
    """Test-only. tests/test_core_dashboard.py runs each test against its
    own freshly migrated Postgres *schema* (app.db.testing.migrated_schema)
    — without this reset, this module-global cache would leak one test's
    section list into the next test's assertions.
    """
    global _risk_sections_cache, _risk_sections_cache_at
    _risk_sections_cache = None
    _risk_sections_cache_at = 0.0

# Score -> tier-key SQL fragments (frontend-contract.md §3.1/§3.3's fixed
# 4-tier banding: >=8 / 6-7 / =5 / <5). Client computes the label/color
# from the raw score the API sends (util.js's tier()); these are ONLY
# used server-side to filter/aggregate by band.
TIER_SQL: dict[str, str] = {
    "red": "llm_score >= 8",
    "yellow": "llm_score >= 6 AND llm_score < 8",
    "green": "llm_score >= 5 AND llm_score < 6",
    "magenta": "llm_score < 5",
}


# ── Dashboard — short term (frontend-contract.md §3.1) ──────────────────


class KpiOut(BaseModel):
    value: str
    label: str
    delta: str = ""
    note: str = ""
    # SHORT-TERM sparkline (dashboard-longterm-spec.md's short-term
    # section): real last-14-days daily counts for this KPI, computed
    # from the same grouped queries days14[] already uses — not a
    # decorative fabricated series. Empty for KPIs with no daily time
    # series to show (e.g. "Awaiting review" is a point-in-time queue
    # depth, not something this app retains a history of).
    vals: list[int] = Field(default_factory=list)


class FunnelStageOut(BaseModel):
    phase: str
    label: str
    value: str
    pct: str
    note: str = ""


class DayVolumeOut(BaseModel):
    date: str
    count: int


class RelMixOut(BaseModel):
    key: str
    count: int


class SectionCountOut(BaseModel):
    label: str
    count: int


class SourceCountOut(BaseModel):
    label: str
    count: int


class DashboardTodayOut(BaseModel):
    pendingCount: int
    approvedCount: int
    todayLabel: str
    kpis: list[KpiOut]
    funnel: list[FunnelStageOut]
    days14: list[DayVolumeOut]
    relMix: list[RelMixOut]
    bySection: list[SectionCountOut]
    topSources: list[SourceCountOut]


# ── Dashboard — long term ───────────────────────────────────────────────
#
# `DashboardTrendsOut` and the eight models only it used (TrendCardOut,
# MonthVolumeOut, MonthCoverageOut, WatchRowOut, WatchGroupOut,
# HeatRowOut, StandingThemeOut, RelMixTrendPointOut) lived here and are
# gone, together with `GET /dashboard/trends` itself — see the note at
# the foot of `app/api/routers/dashboard.py` for the full reasoning. The
# short version: the shipped frontend has read four per-tab endpoints
# (`app/api/schemas/dashboard_long.py`) since the Long-term screen was
# rebuilt, nothing called the old route any more, and what it still
# served was a twelve-month macro series gap-filled from a table with no
# writer — eleven zeroes and one live figure, for each of three
# indicators.
#
# `SourceCountOut` and the rest of the short-term models above are
# shared/unaffected.

# ── Review queue (frontend-contract.md §3.3) ─────────────────────────────


class DupOut(BaseModel):
    id: int
    source: str
    title: str
    url: str = ""


class TopicPeerOut(BaseModel):
    id: int
    source: str
    title: str
    score: float | None = None


class ArticleOut(BaseModel):
    id: int
    title: str
    source: str
    date: str = ""  # ISO yyyy-mm-dd (articles.day) — REVIEW-PERF: added so the queue can
    # date-group client-side without re-deriving a date from `time` (which is HH:MM only).
    time: str = ""
    section: str = ""
    score: float | None = None
    dupCount: int = 0
    isStarred: bool = False
    status: str
    titleEn: str = ""
    url: str = ""
    reason: str = ""
    keywords: list[str] = Field(default_factory=list)
    body: str = ""
    bodyFetched: bool = False
    note: str = ""
    dups: list[DupOut] | None = None
    topicPeers: list[TopicPeerOut] | None = None
    topic: str = ""


class SourceFilterOut(BaseModel):
    name: str
    count: int


class DayCountOut(BaseModel):
    """How many articles match the CURRENT filters on one day.

    The review queue groups by date, and its group headers used to show
    `group.items.length` — the number of that day's rows inside the
    loaded page, not the number that exist. With a 200-row page sorted
    by relevance, 14 August displayed "44" while 543 articles actually
    matched. The header looked like a fact about the day and was really
    a fact about pagination.
    """

    date: str
    count: int


class ArticleListOut(BaseModel):
    items: list[ArticleOut]
    total: int
    sources: list[SourceFilterOut]
    # True per-day totals for the active filters, independent of
    # limit/offset. See DayCountOut.
    days: list[DayCountOut] = Field(default_factory=list)


class ArticlePatchIn(BaseModel):
    status: str | None = None
    starred: bool | None = None
    note: str | None = None


class BulkActionIn(BaseModel):
    ids: list[int]
    action: str  # "approve" | "reject" | "star"


class BulkActionOut(BaseModel):
    updated: int


# ── Export (frontend-contract.md §3.4) ───────────────────────────────────


class ExportPreviewOut(BaseModel):
    rows: int
    summary: str
    filename: str
    counts: dict[str, int]


class ExportDownloadBody(BaseModel):
    """Optional JSON body for `POST /export`. frontend/js/screens/export.js's
    `doExport()` sends every parameter via the query string (no body) —
    that path keeps working unchanged (every field here defaults to
    `None`, meaning "use the query param"). This exists so a JSON-body
    caller (any REST client that doesn't special-case this one route)
    gets the SAME parameters honored, not silently ignored: a query
    param and a body field of the same name must never both be live and
    disagree silently, so the router treats a non-None body field as
    authoritative over its query counterpart, per field.
    """

    filter: str | None = None
    topN: int | None = None
    scope: str | None = None
    from_: str | None = Field(default=None, alias="from")
    to: str | None = None
    sections: str | list[str] | None = None
    columns: str | list[str] | None = None
    sort: str | None = None
    format: str | None = None

    model_config = {"populate_by_name": True}


class ExportPresetIn(BaseModel):
    """A named set of export selections, saved from the Export screen.

    `from`/`to` are here because a preset saved with scope "Custom
    range" USED TO LOSE ITS DATES. frontend/js/screens/export.js posts
    `{ name, ...params() }`, params() includes both dates, this model
    had no such fields, and pydantic dropped them without a word.
    Loading that preset later restored the scope ("Custom range") with
    two blank date fields, and app.services.export.resolve_scope falls
    back to TODAY when a range has no dates, so the preview reported
    today's rows under a heading claiming a quarter. No error at any
    point, which is what made it worth fixing rather than documenting.
    """

    name: str
    note: str = ""
    filter: str = "topn"
    topN: int = 25
    scope: str = "today"
    from_: str | None = Field(default=None, alias="from")
    to: str | None = None
    sections: str | list[str] = ""
    sort: str = "relevance"
    format: str = "xlsx"
    columns: str | list[str] = ""

    model_config = {"populate_by_name": True}

    @model_validator(mode="after")
    def _carry_range_inside_scope(self) -> ExportPresetIn:
        """Fold a custom range into the scope string.

        A scope of "range" with no dates is an incomplete statement, and
        app.services.export.split_scope()/create_preset() unfold this
        again into three stored fields, so nothing downstream has to
        know about the encoding to READ a preset. See that module's
        RANGE_SCOPE_PREFIX comment for the full reasoning; the short
        version is that the three parts of a custom range describe one
        thing and should travel as one value wherever only one value
        fits.
        """
        if self.scope == "range" and (self.from_ or self.to):
            from app.services.export import RANGE_SCOPE_PREFIX  # lazy: keep schemas -> services out of load order

            self.scope = f"{RANGE_SCOPE_PREFIX}{self.from_ or ''}:{self.to or ''}"
        return self


class ExportPresetOut(BaseModel):
    id: str
    name: str
    note: str = ""
    filter: str
    topN: int
    scope: str
    # Serialized as "from"/"to" (FastAPI responds by alias), so a client
    # reads a preset back in exactly the shape it posted one.
    from_: str | None = Field(default=None, alias="from")
    to: str | None = None
    sections: list[str]
    sort: str
    format: str
    columns: list[str] = Field(default_factory=list)

    model_config = {"populate_by_name": True}


# ── Daily Risk Insight brief (frontend-contract.md §3.5) ─────────────────


class BriefBulletOut(BaseModel):
    lead: str
    rest: str = ""


class BriefSectionOut(BaseModel):
    title: str
    fuelLine: str = ""
    bullets: list[BriefBulletOut]
    empty: bool = False


class BriefDraftOut(BaseModel):
    fuel: str
    ready: bool
    sections: list[BriefSectionOut]


class BriefDraftIn(BaseModel):
    fuel: str = ""


class FuelLineIn(BaseModel):
    fuel: str


# ── Search archive (frontend-contract.md §3.6) ───────────────────────────


class SearchResultOut(BaseModel):
    id: int
    source: str
    day: str
    title: str
    titleEn: str = ""
    url: str = ""
    status: str = ""
    body: str = ""


class SearchOut(BaseModel):
    results: list[SearchResultOut]
    total: int
    corpusSize: int
    sources: list[SourceFilterOut] = Field(default_factory=list)
