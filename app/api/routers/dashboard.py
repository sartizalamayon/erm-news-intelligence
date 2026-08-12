"""Admin: the storage dashboard — current usage, ceiling, % used,
projected days remaining, manual purge with dry-run + confirmation, and
the export -> purge -> re-import data-lifecycle journey (ADR §9.2, DoD
#5/#6 of the ADMIN-API mission).

NOT the general app Dashboard screen (frontend-contract.md §3.1-3.2,
short-term/long-term today-and-trends views) — this original stub's own
docstring named that a separate, larger, not-yet-built surface ("the
largest net-new block in the project, likely its own agent"), and the
ADMIN-API mission's context-reading list names only §3.7/§3.8/§3.9/§3.10
— §3.1/§3.2 are out of scope here. Everything below is scoped under
`/dashboard/storage/*` specifically so it can never collide with
whoever builds `/dashboard/today` or `/dashboard/trends` next; this
router's existing bare `/dashboard` prefix (from the original stub) is
left otherwise untouched for that future work.

Backed by `app.services.storage` (ARCH-DB's accounting + purge —
consumed here, never reimplemented; ARCH-DB is concurrently fixing a
preview/delete race in that module, so this router calls its public
functions and inherits that fix automatically once landed) and
`app.services.data_transfer` (export/import).

Every route is gated by `require_admin` (app.api.deps).
"""

from __future__ import annotations

import dataclasses
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from app.api.deps import AuthenticatedUser, get_current_user, require_admin
from app.api.schemas.admin import (
    ExportRequestBody,
    ExportResultOut,
    ImportRequestBody,
    ImportResultOut,
    PurgeRequestBody,
    PurgeResultOut,
    StorageUsageOut,
)
from app.api.schemas.core import (
    DashboardTodayOut,
    DashboardTrendsOut,
    DayVolumeOut,
    FunnelStageOut,
    HeatRowOut,
    KpiOut,
    MonthVolumeOut,
    RelMixOut,
    RelMixTrendPointOut,
    SectionCountOut,
    SourceCountOut,
    StandingThemeOut,
    TrendCardOut,
    get_risk_sections,
)
from app.config import get_settings
from app.db import queries
from app.db.connection import DictConnection, get_connection
from app.db.settings import get_setting, set_setting
from app.services import admin as admin_service
from app.services import macro as macro_service

router = APIRouter(prefix="/dashboard", tags=["dashboard", "admin", "storage"])


@router.get("/storage", response_model=StorageUsageOut)
def get_storage(admin: AuthenticatedUser = Depends(require_admin)) -> StorageUsageOut:
    with get_connection() as conn:
        usage = admin_service.storage_usage(conn)
    return StorageUsageOut(**dataclasses.asdict(usage))


@router.post("/storage/purge", response_model=PurgeResultOut)
def purge(body: PurgeRequestBody, admin: AuthenticatedUser = Depends(require_admin)) -> PurgeResultOut:
    older_than = date.fromisoformat(body.older_than) if body.older_than else None
    with get_connection() as conn:
        result = admin_service.run_purge(
            conn,
            operation=body.operation,
            older_than_days=body.older_than_days,
            older_than=older_than,
            require_exported=body.require_exported,
            confirm=body.confirm,
        )
    return PurgeResultOut(**dataclasses.asdict(result))


@router.post("/storage/export", response_model=ExportResultOut)
def export(body: ExportRequestBody, admin: AuthenticatedUser = Depends(require_admin)) -> ExportResultOut:
    day_from = date.fromisoformat(body.day_from) if body.day_from else None
    day_to = date.fromisoformat(body.day_to) if body.day_to else None
    with get_connection() as conn:
        manifest = admin_service.run_export(
            conn,
            day_from=day_from,
            day_to=day_to,
            review_status=body.review_status,
            mark_exported=body.mark_exported,
            path=body.path,
        )
    return ExportResultOut(
        path=manifest.path,
        articles_exported=manifest.articles_exported,
        bodies_exported=manifest.bodies_exported,
        exported_at=manifest.exported_at.isoformat(),
    )


@router.post("/storage/import", response_model=ImportResultOut)
def import_(body: ImportRequestBody, admin: AuthenticatedUser = Depends(require_admin)) -> ImportResultOut:
    with get_connection() as conn:
        report = admin_service.run_import(conn, path=body.path)
    return ImportResultOut(**dataclasses.asdict(report))


# ─────────────────────────────────────────────────────────────────────────
# General app Dashboard — Short term / Long term (frontend-contract.md
# §3.1/§3.2, frontend/js/screens/dashboard.js's header comment). Picked
# up here per this file's own header note above ("whoever builds
# /dashboard/today or /dashboard/trends next"). Requires only an
# authenticated session (`get_current_user`) — these are user-facing
# analytics, NOT gated by `require_admin` like every route above.
#
# Short-term reads `articles` directly (bounded by raw_retention_days,
# always well over the 14-day window this tab needs — storage-policy.md
# §2). Long-term reads ONLY the rollup_monthly_* / macro_indicator_values
# tables (migrations/0007_rollups.sql), never raw `articles` — exactly
# what makes the 12-month tab affordable and what storage-policy.md §2/§3
# documents as the whole point of those tables. This router does not
# populate the rollups (that write path belongs to whichever job folds a
# day's collected articles into the current month's row, per 0007's own
# header comment); it only reads what is there.
# ─────────────────────────────────────────────────────────────────────────

DHAKA = timezone(timedelta(hours=6))


def _today_dhaka() -> date:
    return datetime.now(DHAKA).date()


def _count(conn: DictConnection, where_sql: str, params: tuple) -> int:
    row = conn.execute(f"SELECT count(*) AS n FROM articles WHERE {where_sql}", params).fetchone()
    return int(row["n"]) if row else 0


def _pct_delta(current: int, previous: int) -> str:
    if not previous:
        return ""
    pct = (current - previous) / previous * 100
    sign = "+" if pct >= 0 else ""
    return f"{sign}{pct:.1f}%"


def _pct_of(part: int, whole: int) -> str:
    if not whole:
        return "0%"
    pct = part / whole * 100
    return f"{pct:.0f}%" if pct >= 10 else f"{pct:.1f}%"


@router.get("/today", response_model=DashboardTodayOut)
def dashboard_today(user: AuthenticatedUser = Depends(get_current_user)) -> DashboardTodayOut:
    today = _today_dhaka()
    yesterday = today - timedelta(days=1)
    day_range14 = [today - timedelta(days=offset) for offset in range(13, -1, -1)]
    day_from14 = day_range14[0]

    with get_connection() as conn:
        # Real last-14-days series, one grouped query per metric instead
        # of 3x14 individual per-day COUNTs — backs both days14[] (as
        # before) and each KPI's new sparkline `vals` (dashboard-longterm-spec.md's
        # short-term section) from the SAME real daily numbers, not a
        # decorative series.
        fetched_by_day = {
            r["d"]: int(r["n"])
            for r in conn.execute(
                "SELECT day AS d, count(*) AS n FROM articles WHERE day BETWEEN %s AND %s GROUP BY day",
                (day_from14, today),
            ).fetchall()
        }
        matched_by_day = {
            r["d"]: int(r["n"])
            for r in conn.execute(
                "SELECT day AS d, count(*) AS n FROM articles WHERE day BETWEEN %s AND %s "
                "AND review_status != 'filtered_out' GROUP BY day",
                (day_from14, today),
            ).fetchall()
        }
        approved_by_day = {
            r["d"]: int(r["n"])
            for r in conn.execute(
                "SELECT reviewed_at::date AS d, count(*) AS n FROM articles WHERE review_status = 'approved' "
                "AND reviewed_at::date BETWEEN %s AND %s GROUP BY reviewed_at::date",
                (day_from14, today),
            ).fetchall()
        }

        fetched_today = fetched_by_day.get(today, 0)
        fetched_yesterday = fetched_by_day.get(yesterday, 0)
        matched_today = matched_by_day.get(today, 0)
        matched_yesterday = matched_by_day.get(yesterday, 0)
        approved_today = approved_by_day.get(today, 0)
        pending_count = _count(conn, "review_status = 'pending'", ())
        scored_today = _count(conn, "day = %s AND llm_score IS NOT NULL", (today,))

        fetched_vals14 = [fetched_by_day.get(d, 0) for d in day_range14]
        matched_vals14 = [matched_by_day.get(d, 0) for d in day_range14]
        approved_vals14 = [approved_by_day.get(d, 0) for d in day_range14]

        sources_total_row = conn.execute("SELECT count(*) AS n FROM sources WHERE enabled").fetchone()
        sources_total = int(sources_total_row["n"]) if sources_total_row else 0
        sources_responded_row = conn.execute(
            "SELECT count(DISTINCT source) AS n FROM articles WHERE day = %s", (today,)
        ).fetchone()
        sources_responded = int(sources_responded_row["n"]) if sources_responded_row else 0

        kpis = [
            KpiOut(
                value=f"{fetched_today:,}",
                label="Fetched today",
                delta=_pct_delta(fetched_today, fetched_yesterday),
                note=f"{sources_responded} of {sources_total} sources responded",
                vals=fetched_vals14,
            ),
            KpiOut(
                value=f"{matched_today:,}",
                label="Keyword matched",
                delta=_pct_delta(matched_today, matched_yesterday),
                note="of everything fetched",
                vals=matched_vals14,
            ),
            # No `vals`: "awaiting review" is a point-in-time queue depth
            # (current pending count), not a daily series this app keeps
            # history of — a sparkline here would have to be fabricated.
            KpiOut(value=f"{pending_count:,}", label="Awaiting review", note="across all matched articles"),
            KpiOut(
                value=f"{approved_today:,}",
                label="Approved today",
                note="ready for export",
                vals=approved_vals14,
            ),
        ]

        funnel = [
            FunnelStageOut(
                phase="Phase 1",
                label="Fetched",
                value=f"{fetched_today:,}",
                pct="100%",
                note=f"Everything published today across {sources_total} sources.",
            ),
            FunnelStageOut(
                phase="Phase 2",
                label="Keyword matched",
                value=f"{matched_today:,}",
                pct=_pct_of(matched_today, fetched_today),
                note="Matched at least one tracked keyword.",
            ),
            FunnelStageOut(
                phase="Phase 3",
                label="Scored & grouped",
                value=f"{scored_today:,}",
                pct=_pct_of(scored_today, fetched_today),
                note="Scored for relevance by the AI stage.",
            ),
            FunnelStageOut(
                phase="Phase 4",
                label="Approved for export",
                value=f"{approved_today:,}",
                pct=_pct_of(approved_today, fetched_today),
                note="Signed off by an analyst today.",
            ),
        ]

        days14 = [DayVolumeOut(date=d.isoformat(), count=matched_by_day.get(d, 0)) for d in day_range14]

        rel_row = conn.execute(
            """
            SELECT
              count(*) FILTER (WHERE llm_score >= 8) AS red,
              count(*) FILTER (WHERE llm_score >= 6 AND llm_score < 8) AS yellow,
              count(*) FILTER (WHERE llm_score >= 5 AND llm_score < 6) AS green,
              count(*) FILTER (WHERE llm_score < 5) AS magenta
            FROM articles WHERE day = %s AND llm_score IS NOT NULL
            """,
            (today,),
        ).fetchone()
        rel_mix = [
            RelMixOut(key="red", count=int((rel_row or {}).get("red") or 0)),
            RelMixOut(key="yellow", count=int((rel_row or {}).get("yellow") or 0)),
            RelMixOut(key="green", count=int((rel_row or {}).get("green") or 0)),
            RelMixOut(key="magenta", count=int((rel_row or {}).get("magenta") or 0)),
        ]

        section_rows = conn.execute(
            """
            SELECT sec AS label, count(*) AS n
            FROM articles a, unnest(string_to_array(a.sections, ', ')) AS sec
            WHERE a.day = %s AND a.sections <> ''
            GROUP BY sec
            """,
            (today,),
        ).fetchall()
        section_counts = {r["label"]: int(r["n"]) for r in section_rows}
        by_section = [SectionCountOut(label=s, count=section_counts.get(s, 0)) for s in get_risk_sections(conn)]

        top_source_rows = conn.execute(
            "SELECT source AS label, count(*) AS n FROM articles WHERE day = %s "
            "GROUP BY source ORDER BY n DESC LIMIT 6",
            (today,),
        ).fetchall()
        top_sources = [SourceCountOut(label=r["label"], count=int(r["n"])) for r in top_source_rows]

    return DashboardTodayOut(
        pendingCount=pending_count,
        approvedCount=approved_today,
        todayLabel=f"{today.day} {today.strftime('%b %Y')}",
        kpis=kpis,
        funnel=funnel,
        days14=days14,
        relMix=rel_mix,
        bySection=by_section,
        topSources=top_sources,
    )


@dataclasses.dataclass(frozen=True)
class _MacroMeta:
    label: str
    unit: str
    decimals: int
    bad: bool


_MACRO_META: dict[str, _MacroMeta] = {
    "taka_usd": _MacroMeta(label="Taka / USD", unit="BDT", decimals=1, bad=True),
    "inflation": _MacroMeta(label="Inflation", unit="%", decimals=1, bad=True),
    "brent_crude": _MacroMeta(label="Brent crude", unit="USD/bbl", decimals=0, bad=True),
    "remittance": _MacroMeta(label="Remittance", unit="USD mn", decimals=1, bad=False),
}

# Remittance: no verified free API exists (Bangladesh Bank exposes
# none — MACRO-DASHBOARD's report). Rather than a fragile scraper, an
# admin types in the latest published figure periodically — same
# `settings` KV-table pattern `relevance_guidance` uses
# (app.services.llm.prompts.RELEVANCE_GUIDANCE_KEY / app.db.settings).
REMITTANCE_VALUE_SETTING_KEY = "macro_remittance_manual_usd_mn"
REMITTANCE_ASOF_SETTING_KEY = "macro_remittance_manual_as_of"


class RemittanceUpdateBody(BaseModel):
    """PUT /dashboard/trends/remittance body — the admin-entered manual
    remittance figure (USD mn). `as_of` is a free-text label (e.g. a
    month name) describing which published figure this is; defaults to
    today's date if not given.
    """

    value: float
    as_of: str | None = None


def _month_start(d: date) -> date:
    return d.replace(day=1)


def _add_months(d: date, delta: int) -> date:
    total = d.year * 12 + (d.month - 1) + delta
    return date(total // 12, total % 12 + 1, 1)


@router.get("/trends", response_model=DashboardTrendsOut)
def dashboard_trends(
    range_param: str = Query("12mo", alias="range"), user: AuthenticatedUser = Depends(get_current_user)
) -> DashboardTrendsOut:
    today = _today_dhaka()
    since_month = _add_months(_month_start(today), -11)
    month_list = [_add_months(since_month, i) for i in range(12)]

    settings = get_settings()

    with get_connection() as conn:
        trend_cards = []
        for indicator, meta in _MACRO_META.items():
            rows = conn.execute(
                "SELECT month, value FROM macro_indicator_values WHERE indicator = %s AND month >= %s ORDER BY month",
                (indicator, since_month),
            ).fetchall()
            by_month = {r["month"]: float(r["value"]) for r in rows}
            vals = [by_month.get(m, 0.0) for m in month_list]

            # MACRO-DASHBOARD: layer a real live (or admin-manual, for
            # remittance) figure onto the CURRENT month's value only —
            # historical months keep reading whatever's in
            # macro_indicator_values (unpopulated today, hence this
            # section previously always showing zero). A live/manual
            # source only ever REPLACES the last element of `vals`; it
            # never fabricates the other 11 months. Every fetch_* call
            # is cached + never raises (app.services.macro), so a downed
            # external API degrades to "rollup" (i.e. today's zero),
            # never a 500.
            status = "rollup"
            as_of: str | None = None
            note: str | None = None

            if indicator == "taka_usd":
                live = macro_service.fetch_usd_bdt()
                if live is not None:
                    vals[-1] = live.value
                    status, as_of = "live", live.fetched_at.isoformat()
                    note = "Live from open.er-api.com (USD → BDT)."
                else:
                    note = "Live fetch unavailable right now — showing the last known figure."
            elif indicator == "inflation":
                live = macro_service.fetch_inflation()
                if live is not None:
                    vals[-1] = live.value
                    status, as_of = "live", live.fetched_at.isoformat()
                    note = "Live from World Bank CPI (annual data, ~1yr publication lag)."
                else:
                    note = "Live fetch unavailable right now — showing the last known figure."
            elif indicator == "brent_crude":
                if not settings.oilpriceapi_key:
                    status = "unconfigured"
                    note = "Needs an oilpriceapi.com API key (Settings.oilpriceapi_key) — not yet configured."
                else:
                    live = macro_service.fetch_brent(settings.oilpriceapi_key)
                    if live is not None:
                        vals[-1] = live.value
                        status, as_of = "live", live.fetched_at.isoformat()
                        note = "Live from oilpriceapi.com."
                    else:
                        note = "Live fetch unavailable right now — showing the last known figure."
            elif indicator == "remittance":
                manual_raw = get_setting(conn, REMITTANCE_VALUE_SETTING_KEY, default="")
                if manual_raw.strip():
                    try:
                        vals[-1] = float(manual_raw)
                        status = "manual"
                        as_of = get_setting(conn, REMITTANCE_ASOF_SETTING_KEY, default="") or None
                    except ValueError:
                        pass
                note = "Manually maintained by an analyst (no free live remittance API exists) — not a live feed."

            trend_cards.append(
                TrendCardOut(
                    label=meta.label,
                    unit=meta.unit,
                    vals=vals,
                    bad=meta.bad,
                    decimals=meta.decimals,
                    status=status,
                    asOf=as_of,
                    note=note,
                )
            )

        section_rows = conn.execute(
            "SELECT month, section, matched_count FROM rollup_monthly_section WHERE month >= %s ORDER BY month",
            (since_month,),
        ).fetchall()
        section_by_month: dict[date, dict[str, int]] = defaultdict(dict)
        for r in section_rows:
            section_by_month[r["month"]][r["section"]] = int(r["matched_count"])
        months12 = [MonthVolumeOut(label=m.strftime("%b"), segments=section_by_month.get(m, {})) for m in month_list]

        theme_rows = conn.execute(
            "SELECT theme, month, article_count, intensity FROM rollup_monthly_theme "
            "WHERE month >= %s ORDER BY theme, month",
            (since_month,),
        ).fetchall()
        theme_totals: dict[str, int] = defaultdict(int)
        theme_series: dict[str, dict[date, float]] = defaultdict(dict)
        for r in theme_rows:
            theme_totals[r["theme"]] += int(r["article_count"])
            theme_series[r["theme"]][r["month"]] = float(r["intensity"])
        top_themes = sorted(theme_totals, key=lambda t: -theme_totals[t])[:6]
        heat_rows = [
            HeatRowOut(label=theme, values=[theme_series[theme].get(m, 0.0) for m in month_list])
            for theme in top_themes
        ]

        # MACRO-DASHBOARD: adaptive threshold. The prior hardcoded 9 could
        # never be satisfied with under 9 months of real rollup history
        # (e.g. 2 months into deployment, "standing" permanently empty).
        # Instead require a theme to appear in a strict MAJORITY of the
        # months that actually have any theme data in this 12-month
        # window (not a fixed 12), with a floor of 2 so one single
        # month's data can never alone qualify as "standing" and become
        # noise on day 1. This scales up on its own as history
        # accumulates: 2 available months -> needs 2; 9 -> needs 5;
        # the full 12-month window -> needs 7 (still well under the old
        # fixed 9, since a strict majority of 12 is 7, not 9).
        available_months = len({r["month"] for r in theme_rows})
        standing_threshold = max(2, available_months // 2 + 1)
        standing_raw = queries.standing_themes(conn, since_month, min_consecutive_months=standing_threshold)
        standing = []
        for row in standing_raw[:6]:
            theme = row["theme"]
            series_vals = [theme_series.get(theme, {}).get(m, 0.0) for m in month_list]
            direction = "flat"
            if series_vals:
                delta = series_vals[-1] - series_vals[0]
                if delta > 0.05:
                    direction = "up"
                elif delta < -0.05:
                    direction = "down"
            standing.append(
                StandingThemeOut(
                    name=theme,
                    note=f"Present in {int(row['months_active'])} of the last 12 months.",
                    count=int(row["total_count"]),
                    months=int(row["months_active"]),
                    dir=direction,
                )
            )

        # Relevance-mix trend (dashboard-longterm-spec.md item 2): monthly
        # share of high-scoring (band='high', i.e. score >= 8) matched
        # coverage, from rollup_monthly_score_band
        # (migrations/0007_rollups.sql: PRIMARY KEY (month, band), no
        # section column) -- see RelMixTrendPointOut's docstring for why
        # this is a global trend, not per-section.
        band_rows = conn.execute(
            "SELECT month, band, article_count FROM rollup_monthly_score_band WHERE month >= %s ORDER BY month",
            (since_month,),
        ).fetchall()
        band_by_month: dict[date, dict[str, int]] = defaultdict(dict)
        for r in band_rows:
            band_by_month[r["month"]][r["band"]] = int(r["article_count"])
        rel_mix_trend = []
        for m in month_list:
            bands = band_by_month.get(m, {})
            total = sum(bands.values())
            high = bands.get("high", 0)
            rel_mix_trend.append(
                RelMixTrendPointOut(
                    label=m.strftime("%b"), highShare=(high / total if total else 0.0), total=total
                )
            )

        # Top sources, 12 months (spec item 5) -- aggregate, from
        # rollup_monthly_source (same "no section column" gap as above).
        source_rows = conn.execute(
            "SELECT source, sum(matched_count) AS n FROM rollup_monthly_source WHERE month >= %s "
            "GROUP BY source ORDER BY n DESC LIMIT 8",
            (since_month,),
        ).fetchall()
        top_sources_12mo = [SourceCountOut(label=r["source"], count=int(r["n"])) for r in source_rows]

    return DashboardTrendsOut(
        trendCards=trend_cards,
        months12=months12,
        heatRows=heat_rows,
        standing=standing,
        relMixTrend=rel_mix_trend,
        topSources=top_sources_12mo,
    )


@router.put("/trends/remittance")
def set_remittance(
    body: RemittanceUpdateBody, admin: AuthenticatedUser = Depends(require_admin)
) -> dict[str, object]:
    """Admin-only: set the manually-maintained remittance figure (USD
    mn) the "Remittance" trend card overlays onto the current month —
    see this module's REMITTANCE_VALUE_SETTING_KEY docstring above for
    why this is a manual entry rather than a live fetch (no verified
    free Bangladesh remittance API exists). Gated by `require_admin`
    like every other write route in this file (storage/purge/export
    above), unlike the read-only `/today` and `/trends` routes which
    only require a session.
    """
    as_of = body.as_of or _today_dhaka().isoformat()
    with get_connection() as conn:
        set_setting(conn, REMITTANCE_VALUE_SETTING_KEY, str(body.value))
        set_setting(conn, REMITTANCE_ASOF_SETTING_KEY, as_of)
    return {"value": body.value, "asOf": as_of}
