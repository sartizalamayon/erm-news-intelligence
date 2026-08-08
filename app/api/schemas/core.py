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

from pydantic import BaseModel, Field

# ── Shared constants (not pydantic — reused across dashboard/review/export/brief) ──

# The 5 fixed risk sections (frontend-contract.md §1/§8 item 6 — observed
# as fixed in the mock; keyword rows are section-scoped, matching
# app/services/matching.py's real section strings, confirmed against
# tests/test_p2_matching.py's literal section names).
RISK_SECTIONS: tuple[str, ...] = ("Geopolitical", "Macroeconomy", "Telco/regulatory", "Political", "Others")

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


# ── Dashboard — long term (frontend-contract.md §3.2) ───────────────────


class TrendCardOut(BaseModel):
    label: str
    unit: str
    vals: list[float]
    bad: bool
    decimals: int


class MonthVolumeOut(BaseModel):
    label: str
    segments: dict[str, int]


class HeatRowOut(BaseModel):
    label: str
    values: list[float]


class StandingThemeOut(BaseModel):
    name: str
    note: str
    count: int
    months: int
    dir: str


class DashboardTrendsOut(BaseModel):
    trendCards: list[TrendCardOut]
    months12: list[MonthVolumeOut]
    heatRows: list[HeatRowOut]
    standing: list[StandingThemeOut]


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


class ArticleListOut(BaseModel):
    items: list[ArticleOut]
    total: int
    sources: list[SourceFilterOut]


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
    name: str
    note: str = ""
    filter: str = "topn"
    topN: int = 25
    scope: str = "today"
    sections: str | list[str] = ""
    sort: str = "relevance"
    format: str = "xlsx"
    columns: str | list[str] = ""


class ExportPresetOut(BaseModel):
    id: str
    name: str
    note: str = ""
    filter: str
    topN: int
    scope: str
    sections: list[str]
    sort: str
    format: str
    columns: list[str] = Field(default_factory=list)


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
