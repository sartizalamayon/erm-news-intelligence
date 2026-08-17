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
    MonthCoverageOut,
    MonthVolumeOut,
    RelMixOut,
    RelMixTrendPointOut,
    SectionCountOut,
    SourceCountOut,
    StandingThemeOut,
    TrendCardOut,
    WatchGroupOut,
    WatchRowOut,
    get_risk_sections,
)
from app.config import get_settings
from app.db import queries, watchlist
from app.db.connection import DictConnection, get_connection
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


def _stale_or_missing(has_stored_current: bool, source_label: str) -> tuple[str, str]:
    """Status and note for a macro card whose live fetch just failed.

    The two cases are NOT the same and used to share one message,
    "Live fetch unavailable right now, showing the last known figure":

      * There IS a stored figure for this month, and it is being shown.
        That sentence is true.
      * There is NO stored figure. `macro_indicator_values` is empty in
        this deployment, so the gap-fill 0.0 was shown instead and the
        card asserted "Inflation 0.0%" as a last known figure that does
        not exist. A plausible-looking number is worse than a blank,
        because nothing about it invites a second look.

    "no_data" tells the frontend to render n/a rather than the fill.
    """
    if has_stored_current:
        return "rollup", f"Couldn't reach {source_label} just now, showing the last stored figure."
    return "no_data", f"Couldn't reach {source_label}, and no earlier figure is stored."


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

        # Only sources that CAN respond count toward the denominator.
        # `sources` holds 51 enabled rows but 6 of them have no
        # `registry_key`, i.e. no fetch plugin exists for them
        # (migrations/0014's own note), so they can never contribute an
        # article. Counting them made the tile read "45 of 51 sources
        # responded" on a perfect run and "0 of 51" here — a denominator
        # that is unreachable by construction, and one that disagrees
        # with the run panel's own "across 45 source(s)".
        sources_total_row = conn.execute(
            "SELECT count(*) AS n FROM sources WHERE enabled AND registry_key IS NOT NULL"
        ).fetchone()
        sources_total = int(sources_total_row["n"]) if sources_total_row else 0
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
}


def _month_start(d: date) -> date:
    return d.replace(day=1)


# ── Long-term: the CRO blocks (migrations/0021) ──────────────────────────
#
# WHO THIS SCREEN IS FOR, and what changed because of it.
#
# The reader is Grameenphone's ERM function and ultimately its Chief Risk
# Officer. What that reader does with a 12-month view is decide which
# exposures belong on a risk register with an owner and a mitigation, and
# which were a bad week. "Coverage volume by section, 12 months" does not
# support that decision: it reports how busy the crawler was, sliced by a
# taxonomy (Geopolitical / Macroeconomy / Political) that is not how
# telecom risk arrives. So four things were added, each tied to a
# question the role actually asks:
#
#   1. "Were we even watching?"  -> `coverage` / `windowNote`.
#      `articles.day` is a PUBLICATION date. A crawl started in month N
#      still finds articles published in N-3, so a 12-month chart mixes
#      monitored months with months represented only by backdated
#      leftovers. Unlabelled, the switch-on of monitoring draws as a
#      hundredfold surge in risk. Everything else on the tab is
#      uninterpretable without this block, so it renders first.
#
#   2. "What is the regulator doing, and on which axis?" -> `watch`
#      group `regulatory`. BTRC actions, spectrum and licensing, telecom
#      tax, SIM rules, service-quality mandates, shutdown/throttling,
#      bandwidth. These are risk-register line items.
#
#   3. "Who else is in the news, and for what?" -> `watch` group
#      `operators`. A peer's fine or licence condition is the best
#      available forecast of your own, since the regulator applies the
#      same rule to everyone.
#
#   4. "Is it getting worse, or just noisier?" -> each row's `dir` +
#      `escalating`. Volume alone is not a risk signal; volume rising
#      WITH severity is. Both are computed between the two most recent
#      MONITORED months and normalized per collection day, because the
#      collector ran 9 days in one month and 4 in the next and comparing
#      raw counts across those compares two different amounts of looking.
#
# Every number below is a rollup value or arithmetic over two of them,
# and each row carries `dirBasis`/`severityNote` stating which numbers
# in words, so nothing on the screen has to be taken on trust.


def _fmt_month(d: date) -> str:
    return d.strftime("%b")


def _round_rate(count: int, days: int) -> float | None:
    """Coverage per collection day, or None when the month was not
    monitored. None becomes "n/a" on the screen: dividing by zero
    collection days has no answer, and 0.0 would read as a measured
    absence of coverage.
    """
    if days <= 0:
        return None
    return round(count / days, 2)


def _watch_groups(
    conn: DictConnection,
    since_month: date,
    month_list: list[date],
    coverage_by_month: dict[date, dict[str, int]],
) -> list[WatchGroupOut]:
    """Build the operator and regulatory boards from
    `rollup_monthly_watch`, read-only, one query for the whole window.
    """
    rows = conn.execute(
        "SELECT month, group_key, entity, article_count, scored_count, high_count, score_sum "
        "FROM rollup_monthly_watch WHERE month >= %s",
        (since_month,),
    ).fetchall()
    by_entity: dict[tuple[str, str], dict[date, dict[str, float]]] = defaultdict(dict)
    for r in rows:
        by_entity[(r["group_key"], r["entity"])][r["month"]] = {
            "article_count": float(r["article_count"]),
            "scored_count": float(r["scored_count"]),
            "high_count": float(r["high_count"]),
            "score_sum": float(r["score_sum"]),
        }

    # Months in the window the collector actually ran in, oldest first.
    # Every comparison below is between the last two of these, never
    # between calendar-adjacent months, because an unmonitored month
    # holds no evidence either way.
    monitored = [m for m in month_list if coverage_by_month.get(m, {}).get("collected_days", 0) > 0]
    latest_month = monitored[-1] if monitored else None
    prev_month = monitored[-2] if len(monitored) > 1 else None

    groups: list[WatchGroupOut] = []
    for group in watchlist.WATCH_GROUPS:
        out_rows: list[WatchRowOut] = []
        for entity in group.entities:
            series = by_entity.get((group.key, entity.key), {})
            counts = [int(series.get(m, {}).get("article_count", 0)) for m in month_list]
            total = sum(counts)
            scored = int(sum(series.get(m, {}).get("scored_count", 0) for m in month_list))
            high = int(sum(series.get(m, {}).get("high_count", 0) for m in month_list))
            score_sum = sum(series.get(m, {}).get("score_sum", 0.0) for m in month_list)
            months_present = sum(1 for m in monitored if series.get(m, {}).get("article_count", 0) > 0)
            monitored_set = set(monitored)
            backdated = sum(c for m, c in zip(month_list, counts, strict=True) if m not in monitored_set)

            latest = int(series.get(latest_month, {}).get("article_count", 0)) if latest_month else 0
            previous = int(series.get(prev_month, {}).get("article_count", 0)) if prev_month else 0
            latest_days = coverage_by_month.get(latest_month, {}).get("collected_days", 0) if latest_month else 0
            prev_days = coverage_by_month.get(prev_month, {}).get("collected_days", 0) if prev_month else 0
            latest_rate = _round_rate(latest, latest_days)
            prev_rate = _round_rate(previous, prev_days)

            direction = "unknown"
            dir_basis = "Needs two monitored months to compare. Only one so far."
            if latest_rate is not None and prev_rate is not None and latest_month and prev_month:
                lm, pm = _fmt_month(latest_month), _fmt_month(prev_month)
                if prev_rate == 0 and latest_rate == 0:
                    direction = "flat"
                elif prev_rate == 0:
                    direction = "up"
                # A 15% deadband: below that, month-to-month wobble at
                # these volumes is not a direction.
                elif latest_rate > prev_rate * 1.15:
                    direction = "up"
                elif latest_rate < prev_rate * 0.85:
                    direction = "down"
                else:
                    direction = "flat"
                dir_basis = (
                    f"{latest_rate} per collection day in {lm} against {prev_rate} in {pm} "
                    f"({latest} over {latest_days} days, then {previous} over {prev_days})."
                )
            elif not monitored:
                dir_basis = "No month in this window was monitored."

            # Severity: a real mean over real scored articles, or n/a.
            # Never a 0.0 stand-in -- on a 0 to 10 relevance scale that
            # reads as "assessed, and harmless".
            avg_score = round(score_sum / scored, 1) if scored else None
            if not total:
                severity_note = "Not seen in this window."
            elif not scored:
                severity_note = f"{total} identified, none scored by the AI stage yet, so no severity."
            else:
                severity_note = f"Mean relevance {avg_score} over {scored} scored of {total} identified."

            # Escalation is the both-axes signal, and it is deliberately
            # hard to trip. Volume per collection day up AND mean
            # severity up, on a base big enough that one article cannot
            # move it. Anything less is volume, which is not risk.
            escalating = False
            if latest_month and prev_month and latest_rate is not None and prev_rate is not None:
                latest_scored = int(series.get(latest_month, {}).get("scored_count", 0))
                prev_scored = int(series.get(prev_month, {}).get("scored_count", 0))
                if latest >= 3 and latest_scored >= 3 and prev_scored >= 1:
                    latest_mean = series[latest_month]["score_sum"] / latest_scored
                    prev_mean = series[prev_month]["score_sum"] / prev_scored
                    escalating = latest_rate > prev_rate and latest_mean > prev_mean

            out_rows.append(
                WatchRowOut(
                    key=entity.key,
                    label=entity.label,
                    note=entity.note,
                    counts=counts,
                    total=total,
                    backdated=backdated,
                    monthsPresent=months_present,
                    latest=latest,
                    previous=previous,
                    latestLabel=_fmt_month(latest_month) if latest_month else "n/a",
                    prevLabel=_fmt_month(prev_month) if prev_month else "n/a",
                    latestRate=latest_rate,
                    prevRate=prev_rate,
                    dir=direction,
                    dirBasis=dir_basis,
                    avgScore=avg_score,
                    scored=scored,
                    high=high,
                    escalating=escalating,
                    severityNote=severity_note,
                )
            )
        # Busiest first, so the board leads with what is actually moving.
        out_rows.sort(key=lambda r: (-r.total, r.label))
        groups.append(WatchGroupOut(key=group.key, title=group.title, blurb=group.blurb, rows=out_rows))
    return groups


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
            # Whether the CURRENT month has a genuine stored figure, as
            # opposed to the 0.0 the line above fills gaps with.
            #
            # `macro_indicator_values` is empty in this deployment, so
            # every month of every indicator is 0.0. When a live fetch
            # also fails, the card rendered "Inflation 0.0%" under the
            # caption "showing the last known figure" — but there is no
            # last known figure, and 0.0 is a plausible-looking
            # inflation rate rather than an obvious blank. A fill value
            # presented as a measurement is the same defect as a full
            # progress bar labelled 0%.
            has_stored_current = month_list[-1] in by_month

            # MACRO-DASHBOARD: layer a real live figure onto the CURRENT
            # month's value only — historical months keep reading
            # whatever's in macro_indicator_values (unpopulated today,
            # hence this section previously always showing zero). A live
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
                    status, note = _stale_or_missing(has_stored_current, "open.er-api.com")
            elif indicator == "inflation":
                live = macro_service.fetch_inflation()
                if live is not None:
                    vals[-1] = live.value
                    status, as_of = "live", live.fetched_at.isoformat()
                    note = "Live from World Bank CPI (annual data, ~1yr publication lag)."
                else:
                    status, note = _stale_or_missing(has_stored_current, "the World Bank CPI API")
            elif indicator == "brent_crude":
                if not settings.oilpriceapi_key:
                    status = "unconfigured"
                    note = "Needs an oilpriceapi.com API key (Settings.oilpriceapi_key), not yet configured."
                else:
                    live = macro_service.fetch_brent(settings.oilpriceapi_key)
                    if live is not None:
                        vals[-1] = live.value
                        status, as_of = "live", live.fetched_at.isoformat()
                        note = "Live from oilpriceapi.com."
                    else:
                        status, note = _stale_or_missing(has_stored_current, "oilpriceapi.com")

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

        # Coverage first: every series below is read inside this frame.
        coverage_rows = conn.execute(
            "SELECT month, article_count, matched_count, scored_count, collected_count, collected_days "
            "FROM rollup_monthly_coverage WHERE month >= %s ORDER BY month",
            (since_month,),
        ).fetchall()
        coverage_by_month: dict[date, dict[str, int]] = {
            r["month"]: {
                "article_count": int(r["article_count"]),
                "matched_count": int(r["matched_count"]),
                "scored_count": int(r["scored_count"]),
                "collected_count": int(r["collected_count"]),
                "collected_days": int(r["collected_days"]),
            }
            for r in coverage_rows
        }
        coverage = [
            MonthCoverageOut(
                label=_fmt_month(m),
                month=m.isoformat(),
                monitored=coverage_by_month.get(m, {}).get("collected_days", 0) > 0,
                collectedDays=coverage_by_month.get(m, {}).get("collected_days", 0),
                articles=coverage_by_month.get(m, {}).get("article_count", 0),
                collected=coverage_by_month.get(m, {}).get("collected_count", 0),
                matched=coverage_by_month.get(m, {}).get("matched_count", 0),
            )
            for m in month_list
        ]
        monitored_months = sum(1 for c in coverage if c.monitored)
        collection_days = sum(c.collectedDays for c in coverage)
        if monitored_months == 0:
            window_note = (
                "No month in this window has any collection activity recorded, so nothing below is a trend. "
                "Run a collection, or backfill the rollups, and this tab fills in."
            )
        else:
            monitored_labels = ", ".join(c.label for c in coverage if c.monitored)
            window_note = (
                f"Collection ran on {collection_days} days across {monitored_months} of these 12 months "
                f"({monitored_labels}). The other months hold only backdated articles a later crawl "
                "happened to surface, so they are not a sample of that month's news and are drawn as "
                "not monitored rather than as zero."
            )

        section_rows = conn.execute(
            "SELECT month, section, matched_count FROM rollup_monthly_section WHERE month >= %s ORDER BY month",
            (since_month,),
        ).fetchall()
        section_by_month: dict[date, dict[str, int]] = defaultdict(dict)
        for r in section_rows:
            section_by_month[r["month"]][r["section"]] = int(r["matched_count"])
        months12 = [
            MonthVolumeOut(
                label=_fmt_month(m),
                segments=section_by_month.get(m, {}),
                monitored=coverage_by_month.get(m, {}).get("collected_days", 0) > 0,
            )
            for m in month_list
        ]

        watch = _watch_groups(conn, since_month, month_list, coverage_by_month)

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
                    # NOT "of the last 12 months". Only `available_months`
                    # of this window carry any theme data at all, and in
                    # this deployment that is 2, so "present in 2 of the
                    # last 12 months" reads as a theme that faded when it
                    # is in fact a theme present every month we looked.
                    # The denominator has to be the months with data.
                    note=(
                        f"Present in {int(row['months_active'])} of the "
                        f"{available_months} months with theme data."
                    ),
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
                    label=m.strftime("%b"),
                    highShare=(high / total if total else 0.0),
                    total=total,
                    monitored=coverage_by_month.get(m, {}).get("collected_days", 0) > 0,
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
        coverage=coverage,
        monitoredMonths=monitored_months,
        windowNote=window_note,
        watch=watch,
    )
