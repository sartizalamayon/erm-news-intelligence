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
whoever builds `/dashboard/today` next; this router's existing bare
`/dashboard` prefix (from the original stub) is left otherwise
untouched for that future work. (That future work arrived and then
split: `/dashboard/today` is below, and the Long-term tabs live in
`app/api/routers/dashboard_long.py`. The single `/dashboard/trends`
payload that used to sit here is gone — see the note at the foot of
this file for what replaced it and why it was deleted rather than
left dark.)

Backed by `app.services.storage` (ARCH-DB's accounting + purge —
consumed here, never reimplemented; ARCH-DB is concurrently fixing a
preview/delete race in that module, so this router calls its public
functions and inherits that fix automatically once landed) and
`app.services.data_transfer` (export/import).

Every route is gated by `require_admin` (app.api.deps).
"""

from __future__ import annotations

import dataclasses
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Depends

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
    DayVolumeOut,
    FunnelStageOut,
    KpiOut,
    RelMixOut,
    SectionCountOut,
    SourceCountOut,
    get_risk_sections,
)
from app.db.connection import DictConnection, get_connection
from app.services import admin as admin_service
from app.services.collector import enabled_source_classes

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
# General app Dashboard — Short term (frontend-contract.md §3.1,
# frontend/js/screens/dashboard.js's header comment). Picked up here per
# this file's own header note above ("whoever builds /dashboard/today
# next"). Requires only an authenticated session (`get_current_user`) —
# these are user-facing analytics, NOT gated by `require_admin` like
# every route above.
#
# Short-term reads `articles` directly (bounded by raw_retention_days,
# always well over the 14-day window this tab needs — storage-policy.md
# §2). The Long-term tabs read the rollup_monthly_* tables instead, and
# live in `app/api/routers/dashboard_long.py`; neither this router nor
# that one POPULATES a rollup (that write path is `app/db/rollups.py`,
# called once per batch run from `app.batch.run_daily`).
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
        # TWO different numbers, deliberately. The Short-term dashboard is
        # a TODAY view — every other tile on it is scoped to today — but
        # this one reported the all-days pending queue, so on a fresh day
        # it read "0 fetched / 0 matched / 10,723 awaiting review", which
        # invites the reader to think today produced 10,723 reviewables.
        # The headline is now today's; the standing backlog is kept as
        # the note so it is not lost (the header badge shows it too).
        pending_count = _count(conn, "review_status = 'pending' AND COALESCE(is_primary, TRUE) = TRUE", ())
        pending_today = _count(
            conn, "day = %s AND review_status = 'pending' AND COALESCE(is_primary, TRUE) = TRUE", (today,)
        )
        scored_today = _count(conn, "day = %s AND llm_score IS NOT NULL", (today,))

        fetched_vals14 = [fetched_by_day.get(d, 0) for d in day_range14]
        matched_vals14 = [matched_by_day.get(d, 0) for d in day_range14]
        approved_vals14 = [approved_by_day.get(d, 0) for d in day_range14]

        # Only sources that CAN respond count toward the denominator —
        # counting rows with no fetch plugin behind them made the tile
        # read "45 of 51 sources responded" on a perfect run, a
        # denominator unreachable by construction and one that disagreed
        # with the run panel's own "across 45 source(s)".
        #
        # This is now asked of the collector itself rather than of the
        # `sources` table, and the difference is not cosmetic. The
        # previous query counted `enabled AND registry_key IS NOT NULL`,
        # and no migration ever INSERTs into `sources` — the rows arrive
        # from the legacy import, AFTER the migration chain that was
        # supposed to backfill their keys has already run and been
        # checksummed. Every row therefore carried a NULL key, this
        # count was 0, and the tile announced "0 of 0 sources responded"
        # and "0 sources are configured and enabled" on a day 45 plugins
        # had just run and filed articles. Nothing about the number
        # invited a second look; it just read as a quiet day.
        #
        # `enabled_source_classes` is the exact set `run_collection`
        # fetches from: the code registry, minus what the table vetoes.
        # Asking the one function makes this tile agree with the run
        # panel by construction instead of by two queries happening to
        # agree, and it is correct on an empty, unkeyed or fully-keyed
        # `sources` table alike. (The keys still get backfilled — see
        # `collector.reconcile_source_registry_keys` — because the
        # enable/disable toggle and per-source diagnostics genuinely
        # need them; this tile no longer depends on that having
        # happened.)
        sources_total = len(enabled_source_classes(conn))
        sources_responded_row = conn.execute(
            "SELECT count(DISTINCT source) AS n FROM articles WHERE day = %s", (today,)
        ).fetchone()
        sources_responded = int(sources_responded_row["n"]) if sources_responded_row else 0

        # Before today's collection has run, EVERY day-over-day delta is
        # -100.0% — true arithmetic, useless information, and alarming
        # in red at the top of the screen. A day with nothing fetched
        # yet has not underperformed; it has not started. Deltas are
        # suppressed until the day has any data at all.
        day_has_started = fetched_today > 0

        kpis = [
            KpiOut(
                value=f"{fetched_today:,}",
                label="Fetched today",
                delta=_pct_delta(fetched_today, fetched_yesterday) if day_has_started else "",
                note=(
                    f"{sources_responded} of {sources_total} sources responded"
                    if day_has_started
                    else f"no collection run yet today · {sources_total} sources ready"
                ),
                vals=fetched_vals14,
            ),
            KpiOut(
                value=f"{matched_today:,}",
                label="Keyword matched",
                delta=_pct_delta(matched_today, matched_yesterday) if day_has_started else "",
                note=_pct_of(matched_today, fetched_today) + " of everything fetched"
                if day_has_started
                else "of everything fetched",
                vals=matched_vals14,
            ),
            # No `vals`: "awaiting review" is a point-in-time queue depth,
            # not a daily series this app keeps history of — a sparkline
            # here would have to be fabricated.
            KpiOut(
                value=f"{pending_today:,}",
                label="Awaiting review",
                note=(
                    f"from today · {pending_count:,} across all days"
                    if pending_count != pending_today
                    else "from today"
                ),
            ),
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
                # "100%" was hardcoded, so an empty day drew a full bar
                # labelled 100% above three zeroes. 100% OF NOTHING is
                # not a meaningful figure to show.
                pct="100%" if day_has_started else "0%",
                note=(
                    f"Everything published today across {sources_total} sources."
                    if day_has_started
                    else f"Nothing collected yet today. {sources_total} sources are configured and enabled."
                ),
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


# ── GET /dashboard/trends — REMOVED ─────────────────────────────────────
#
# The original single-payload Long-term tab. It was replaced by the four
# per-tab endpoints in `app/api/routers/dashboard_long.py`
# (/dashboard/long/{risk,market,economy,telecom}), which the shipped
# frontend has read since; nothing called this route any more.
#
# It is deleted rather than left dark because of what it served while it
# sat there. Its three macro cards built a twelve-month series as
# `[by_month.get(m, 0.0) for m in month_list]` out of
# `macro_indicator_values`, a table with NO writer anywhere in this
# codebase -- `app/db/rollups.py` puts it explicitly out of scope. So
# every response carried eleven zeroes plus one live figure, for
# Taka/USD, inflation AND Brent, and the route's own comments said so.
# A fabricated series behind an auth cookie is still fabricated, and the
# standing instruction for this deploy is that no dummy data may remain
# anywhere in the product.
#
# What went with it, and where the equivalent lives now:
#   * the macro cards        -> GET /dashboard/long/economy, which reads
#                               live sources and the economy axes and
#                               labels each figure's provenance.
#   * the watchlist boards   -> GET /dashboard/long/telecom.
#   * coverage / months12 /  -> the same rollup reads, per tab, in
#     heat / standing /         dashboard_long.py, each carrying its own
#     relMixTrend / topSources  window chip.
#
# `DashboardTrendsOut` and the eight response models only it used are
# gone from `app.api.schemas.core` in the same change. One caller
# remains in the tree and is a cosmetic one this change does not own:
# `frontend/js/screens/dashboard.js` still names "GET /dashboard/trends"
# in the label it renders on an error banner. Flagged in this task's
# report.
