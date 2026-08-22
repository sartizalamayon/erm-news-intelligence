"""The rebuilt Long-term dashboard — four endpoints, one per sub-tab,
plus the watchlist add-flow the Telecom tab hosts.

WHY NOT IN dashboard.py
───────────────────────
`app/api/routers/dashboard.py` already carries the storage-admin routes,
the Short-term tab and the original `GET /dashboard/trends`. That
endpoint still exists and still serves its original shape — the
Short-term tab and its tests depend on it — but nothing on the rebuilt
Long-term screen reads it any more. Splitting the new routes out keeps
the old ones untouched and makes the tab-per-endpoint structure obvious
from the file layout.

ONE ENDPOINT PER TAB, ON PURPOSE
────────────────────────────────
The old long-term view was one request carrying everything, which was
affordable while everything came from the monthly rollups. Three of the
four tabs are now backed by data those rollups never had — real
exchange history, the economy axes, and the individual headlines behind
every count — and a reader looks at one tab at a time. Opening Risk
lines should not pay for the exchange fetch.

THE WINDOW
──────────
News-derived boards read the months the collector actually ran in, not
the calendar. `_window()` resolves that once per request from
`rollup_monthly_coverage`, and every board built from it prints the
resulting span. Market and macro figures do NOT use it: they come from
sources that keep running when the collector does not.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Path

from app.api.deps import AuthenticatedUser, get_current_user, require_admin
from app.api.schemas.dashboard_long import (
    BoardOut,
    BoardRowOut,
    EconomyTabOut,
    EvidenceOut,
    GuardOut,
    IndexSeriesOut,
    InstrumentOut,
    MacroCardOut,
    MarketTabOut,
    MonthRowOut,
    ProvenanceOut,
    RiskLineOut,
    RiskTabOut,
    TelecomTabOut,
    WatchCreateBody,
    WatchEntityOut,
    WatchlistOut,
    WatchPreviewBody,
    WatchPreviewOut,
    WatchPreviewSampleOut,
)
from app.config import get_settings
from app.db import econaxes, watchlist
from app.db.connection import DictConnection, get_connection
from app.errors import ValidationError
from app.services import evidence as evidence_service
from app.services import macro as macro_service
from app.services import market as market_service
from app.services import risklines as risklines_service
from app.services import watchlist_admin

router = APIRouter(tags=["dashboard"])

# How many critical items ride along with each board row. Three is what
# the row can show without becoming a page of its own; the row states
# "3 of N shown" whenever N is larger, and the review queue carries the
# rest. It also bounds the payload: a board of 15 rows can never return
# more than 45 articles however loud the month was.
CRIT_PER_ROW = 3

# The two instruments the exchange panel carries. Banglalink and Teletalk
# are unlisted, so market data covers two of five operators and the panel
# says so rather than implying it covers the sector.
MARKET_CODES = ("GP", "ROBI")

# Which context guard the add-flow tests a proposed term against, per
# board. Chosen for the operator rather than asked of them: someone
# adding a wallet should not have to know the guard vocabulary exists to
# discover that নগদ is also the ordinary word for cash. `context` in the
# request still overrides this.
_DEFAULT_GUARD_FOR_GROUP = {"operators": "telecom", "regulatory": "telecom", "mfs": "mfs"}


def _today_dhaka() -> date:
    """Asia/Dhaka is UTC+6 year-round (no DST), so a fixed offset is
    correct here and avoids a tz database dependency for one call.
    """
    return (datetime.now(timezone.utc) + timedelta(hours=6)).date()


def _month_start(d: date) -> date:
    return d.replace(day=1)


def _add_months(d: date, delta: int) -> date:
    total = d.year * 12 + (d.month - 1) + delta
    return date(total // 12, total % 12 + 1, 1)


class _Window:
    """The span the news-derived boards actually cover."""

    def __init__(self, since: date, monitored: list[date], days: dict[date, int]) -> None:
        self.since = since
        self.monitored = monitored
        self.days = days
        self.collection_days = sum(days.get(m, 0) for m in monitored)

    @property
    def label(self) -> str:
        if not self.monitored:
            return "No monitored months yet"
        first, last = self.monitored[0], self.monitored[-1]
        span = first.strftime("%b %Y") if first == last else f"{first.strftime('%b')}–{last.strftime('%b %Y')}"
        return f"{span} · {self.collection_days} collection days"

    @property
    def since_effective(self) -> date:
        """Read from the first MONITORED month, not the calendar start.

        Months before the collector ran hold only backdated articles a
        later crawl happened to surface. Counting them would mix a
        sample of the news with an accident of crawling.
        """
        return self.monitored[0] if self.monitored else self.since


def _window(conn: DictConnection) -> _Window:
    today = _today_dhaka()
    since = _add_months(_month_start(today), -11)
    rows = conn.execute(
        "SELECT month, collected_days FROM rollup_monthly_coverage WHERE month >= %s ORDER BY month",
        (since,),
    ).fetchall()
    days = {r["month"]: int(r["collected_days"]) for r in rows}
    monitored = [m for m in sorted(days) if days[m] > 0]
    return _Window(since, monitored, days)


def _evidence_out(items: list[evidence_service.EvidenceItem]) -> list[EvidenceOut]:
    return [
        EvidenceOut(
            articleId=i.article_id,
            score=i.score,
            band=i.band,
            title=i.title,
            source=i.source,
            day=i.day.isoformat(),
            reason=i.reason,
        )
        for i in items
    ]


def _board_row(
    conn: DictConnection,
    *,
    key: str,
    name: str,
    note: str,
    where: str,
    params: list[str],
    since: date,
    is_us: bool = False,
    is_new: bool = False,
) -> BoardRowOut:
    stats = evidence_service.board_stats(conn, where, params, since=since)
    crit = (
        evidence_service.critical_items(conn, where, params, since=since, limit=CRIT_PER_ROW)
        if stats.critical
        else []
    )
    return BoardRowOut(
        key=key,
        name=name,
        note=note,
        items=stats.items,
        scored=stats.scored,
        critical=stats.critical,
        # Rounded here, at the boundary, not in the service: the service
        # keeps full precision for anything that needs to compare means,
        # and the wire carries the one decimal the screen actually shows.
        mean=round(stats.mean, 1) if stats.mean is not None else None,
        isUs=is_us,
        isNew=is_new,
        crit=_evidence_out(crit),
        criticalTruncated=stats.critical > len(crit),
        # The count is on the books but the articles behind it have aged
        # past the raw-retention window. Distinct from having nothing to
        # show: one is a quiet row, the other is a row we can no longer
        # open.
        critExpired=bool(stats.critical) and not crit,
    )


def _sorted_rows(rows: list[BoardRowOut]) -> list[BoardRowOut]:
    """Mean severity descending, unscored rows last.

    Deliberately NOT by volume. Ordering these boards by item count puts
    the loudest axis on top, and severity runs the other way here: the
    axes with the fewest items score highest, and a volume ordering
    buries exactly the rows a risk officer needs first.
    """
    return sorted(rows, key=lambda r: (r.mean is None, -(r.mean or 0.0), -r.critical))


# ── Risk lines ───────────────────────────────────────────────────────


@router.get("/dashboard/long/risk", response_model=RiskTabOut)
def long_risk(user: AuthenticatedUser = Depends(get_current_user)) -> RiskTabOut:
    settings = get_settings()
    with get_connection() as conn:
        win = _window(conn)
        lines = risklines_service.build_risk_lines(
            conn,
            since=win.since_effective,
            monitored=win.monitored,
            coverage_days=win.days,
            news_window=win.label,
            brent_key=settings.oilpriceapi_key,
        )
        moved = evidence_service.critical_items(
            conn, "TRUE", [], since=win.since_effective, limit=5, min_score=8.0
        )
        totals_row = conn.execute(
            "SELECT coalesce(sum(article_count), 0) AS articles, coalesce(sum(matched_count), 0) AS matched, "
            "coalesce(sum(scored_count), 0) AS scored FROM rollup_monthly_coverage WHERE month = ANY(%s)",
            (win.monitored or [win.since],),
        ).fetchone()
        # An aggregate with no GROUP BY always returns one row, but the
        # driver's signature does not promise that, and a provenance line
        # is not worth a 500 if it ever changes.
        totals = totals_row or {"articles": 0, "matched": 0, "scored": 0}
        market_days = market_service.stored_day_count(conn, MARKET_CODES[0])

    news = (
        f"Collection ran on {win.collection_days} days across {len(win.monitored)} months — "
        f"{int(totals['articles']):,} articles read, {int(totals['matched']):,} matched, "
        f"{int(totals['scored']):,} scored. Months the collector never ran in are not drawn."
        if win.monitored
        else "No month in this window has any collection activity recorded, so nothing news-derived is a trend yet."
    )
    market = (
        f"DSE day-end archive, {market_days} stored trading days. Independent of collection — "
        "these do not thin out when the crawler misses a day."
        if market_days
        else "No exchange history is stored yet. The market tab fills in once the fetch has run."
    )
    return RiskTabOut(
        lines=[
            RiskLineOut(
                key=ln.key,
                label=ln.label,
                direction=ln.direction,
                directionLabel=ln.direction_label,
                value=ln.value,
                unit=ln.unit,
                sub=ln.sub,
                evidence=ln.evidence,
                window=ln.window,
                windowKind=ln.window_kind,
                tone=ln.tone,
                spark=list(ln.spark),
            )
            for ln in lines
        ],
        moved=_evidence_out(moved),
        provenance=ProvenanceOut(
            market=market,
            news=news,
            unavailable=(
                "Operator subscriber share and ARPU: BTRC has moved the page that carried them. "
                "A monthly BDT series: no free publisher offers one. Neither is guessed at here."
            ),
        ),
    )


# ── Market ───────────────────────────────────────────────────────────


@router.get("/dashboard/long/market", response_model=MarketTabOut)
def long_market(user: AuthenticatedUser = Depends(get_current_user)) -> MarketTabOut:
    since = _add_months(_month_start(_today_dhaka()), -11)
    with get_connection() as conn:
        instruments: list[InstrumentOut] = []
        series: list[IndexSeriesOut] = []
        closes_by_code: dict[str, dict[date, float]] = {}
        months: list[date] = []

        for code in MARKET_CODES:
            q = market_service.latest_quote(conn, code)
            rows = market_service.monthly_closes(conn, code, since)
            closes_by_code[code] = {r.month: r.close for r in rows}
            months = sorted(set(months) | set(closes_by_code[code]))
            if q is None:
                continue
            span = q.week52_high - q.week52_low
            instruments.append(
                InstrumentOut(
                    code=q.code,
                    name=q.name,
                    last=q.last,
                    changePct=q.change_pct,
                    dayLow=q.day_low,
                    dayHigh=q.day_high,
                    w52Low=q.week52_low,
                    w52High=q.week52_high,
                    marketCapMn=q.market_cap_mn,
                    freeFloatMn=q.free_float_mn,
                    asOf=q.as_of.isoformat(),
                    belowHighPct=((q.week52_high - q.last) / q.week52_high * 100.0) if q.week52_high else 0.0,
                    rangePos=((q.last - q.week52_low) / span) if span > 0 else 0.0,
                )
            )
            if len(rows) >= 2 and rows[0].close > 0:
                base = rows[0].close
                series.append(
                    IndexSeriesOut(
                        code=code,
                        label=q.name.split()[0] if q.name else code,
                        values=[round(r.close / base * 100.0, 2) for r in rows],
                    )
                )

        trading_days = market_service.stored_day_count(conn, MARKET_CODES[0])

    month_rows: list[MonthRowOut] = []
    for i, m in enumerate(months):
        closes: dict[str, float | None] = {}
        moms: dict[str, float | None] = {}
        for code in MARKET_CODES:
            cur = closes_by_code.get(code, {}).get(m)
            closes[code] = cur
            prev = closes_by_code.get(code, {}).get(months[i - 1]) if i else None
            moms[code] = ((cur - prev) / prev * 100.0) if (cur is not None and prev) else None
        month_rows.append(MonthRowOut(label=m.strftime("%b %Y"), closes=closes, moms=moms))

    as_of = max((i.asOf for i in instruments), default="")
    return MarketTabOut(
        instruments=instruments,
        labels=[m.strftime("%b") for m in months],
        series=series,
        months=month_rows,
        tradingDays=trading_days,
        asOf=as_of,
        window=(f"{months[0].strftime('%b %Y')} – {months[-1].strftime('%b %Y')}" if months else ""),
        note=(
            "Us against the only listed peer, indexed to 100 at the start of the window. "
            "Banglalink and Teletalk are unlisted, so this covers two of five operators — read it as us "
            "against that peer, not as the sector."
        ),
    )


# ── Economy ──────────────────────────────────────────────────────────


def _macro_cards() -> list[MacroCardOut]:
    settings = get_settings()
    cards: list[MacroCardOut] = []

    usd = macro_service.fetch_usd_bdt()
    annual = macro_service.fetch_annual_exchange_rate()
    cards.append(
        MacroCardOut(
            key="taka_usd",
            label="Taka / USD",
            value=f"{usd.value:.2f}" if usd else "—",
            unit="৳/USD",
            status="live" if usd else "no_data",
            asOf=usd.fetched_at.date().isoformat() if usd else "",
            note=(
                (
                    f"open.er-api.com, read at load. World Bank puts the {annual.year} annual average at "
                    f"{annual.value:.2f} — no free source publishes a monthly series."
                )
                if usd and annual
                else "open.er-api.com, read at load. No monthly BDT series is published free."
                if usd
                else "The exchange-rate source could not be reached."
            ),
        )
    )

    brent = macro_service.fetch_brent(settings.oilpriceapi_key) if settings.oilpriceapi_key else None
    cards.append(
        MacroCardOut(
            key="brent_crude",
            label="Brent crude",
            value=f"{brent.value:.2f}" if brent else "—",
            unit="USD/bbl",
            status="live" if brent else ("unconfigured" if not settings.oilpriceapi_key else "no_data"),
            asOf=brent.fetched_at.date().isoformat() if brent else "",
            note=(
                "oilpriceapi.com, live. Feeds the import bill, the power tariff and, through both, "
                "tower operating cost."
                if brent
                else "Needs an oilpriceapi.com API key, not yet configured."
                if not settings.oilpriceapi_key
                else "The Brent source could not be reached."
            ),
        )
    )

    infl = macro_service.fetch_inflation()
    cards.append(
        MacroCardOut(
            key="inflation",
            label="CPI inflation",
            value=f"{infl.value:.2f}" if infl else "—",
            unit="%",
            status="annual" if infl else "no_data",
            asOf=infl.fetched_at.date().isoformat() if infl else "",
            note=(
                "World Bank, annual, with roughly a year of publication lag. It cannot move month to month, "
                "so it is shown as a year figure and not as a trend."
                if infl
                else "The World Bank CPI source could not be reached."
            ),
        )
    )
    return cards


@router.get("/dashboard/long/economy", response_model=EconomyTabOut)
def long_economy(user: AuthenticatedUser = Depends(get_current_user)) -> EconomyTabOut:
    with get_connection() as conn:
        win = _window(conn)
        rows: list[BoardRowOut] = []
        for axis in econaxes.ECON_AXES:
            where, params = econaxes.axis_where_sql(axis)
            rows.append(
                _board_row(
                    conn,
                    key=axis.key,
                    name=axis.label,
                    note=axis.note,
                    where=where,
                    params=params,
                    since=win.since_effective,
                )
            )
    rows = _sorted_rows(rows)
    loud = max(rows, key=lambda r: r.items, default=None)
    sharp = next((r for r in rows if r.mean is not None), None)
    verdict = ""
    if loud is not None and sharp is not None and loud.key != sharp.key:
        verdict = (
            f"{loud.name} produces the most items of any axis at {loud.items:,} and averages "
            f"{loud.mean:.1f}; {sharp.name} averages {sharp.mean:.1f} with {sharp.critical} critical items. "
            "Ranking these by volume would put the quietest axis on top."
        )
    return EconomyTabOut(
        macro=_macro_cards(),
        board=BoardOut(
            key="econ",
            title="Where the economy signal concentrates",
            blurb=(
                "Six economic axes the scoring rubric treats as top-tier, ranked by mean severity. Open any row "
                "to read the items behind it — the same anchoring as the telecom boards, because an economy "
                "number is defended the same way."
            ),
            window=win.label,
            windowKind="news",
            rows=rows,
            verdict=verdict,
        ),
    )


# ── Telecom watch ────────────────────────────────────────────────────

_GROUP_BLURBS = {
    "operators": (
        "The licensed mobile operators and the satellite entrant, ranked by mean severity. A peer's fine, "
        "outage or licence condition is the best available forecast of our own, because the regulator applies "
        "the same rule to everyone."
    ),
    "regulatory": (
        "The axes along which regulation reaches an operator here, ranked by mean severity rather than volume. "
        "The sharpest lines are usually the quietest ones."
    ),
    "mfs": (
        "The mobile-financial-services players competing for the same subscriber wallet. The scoring rubric "
        "already treats MFS as top-tier; this is the board that makes their moves countable."
    ),
}


def _group_board(conn: DictConnection, group_key: str, win: _Window) -> BoardOut:
    pairs = [(g, e) for g, e in watchlist.all_entities_with_custom(conn) if g.key == group_key]
    rows: list[BoardRowOut] = []
    for group, entity in pairs:
        where, params = watchlist.entity_where_sql(entity)
        rows.append(
            _board_row(
                conn,
                key=entity.key,
                name=entity.label,
                note=entity.note,
                where=where,
                params=params,
                since=win.since_effective,
                is_us=entity.key == "grameenphone",
                is_new=group.key == "mfs" or entity.key == "airtel",
            )
        )
    title = pairs[0][0].title if pairs else group_key.title()
    return BoardOut(
        key=group_key,
        title=title,
        blurb=_GROUP_BLURBS.get(group_key, ""),
        window=win.label,
        windowKind="news",
        rows=_sorted_rows(rows),
    )


@router.get("/dashboard/long/telecom", response_model=TelecomTabOut)
def long_telecom(user: AuthenticatedUser = Depends(get_current_user)) -> TelecomTabOut:
    with get_connection() as conn:
        win = _window(conn)
        peers = _group_board(conn, "operators", win)
        wallets = _group_board(conn, "mfs", win)
        regulatory = _group_board(conn, "regulatory", win)

        # Operator initiatives: the highest-scoring items tagged to ANY
        # operator, which is what surfaces a launch or a partnership. No
        # event-type classifier exists yet, so a fine and a launch both
        # land here and the reader separates them by reading the reason —
        # which is exactly why the reason is on the row.
        per_operator: list[tuple[str, list[str]]] = []
        for group, entity in watchlist.all_entities_with_custom(conn):
            if group.key != "operators":
                continue
            per_operator.append(watchlist.entity_where_sql(entity))
        # The whole disjunction gets ONE more layer of parentheses. Without
        # it the caller's "day >= %s AND llm_score >= %s AND " prefix binds
        # to the FIRST arm only, because AND binds tighter than OR — every
        # other operator then bypasses both the window and the score floor,
        # and unscored rows come back with a NULL score. That is exactly
        # the bug this line was written to fix.
        # ONE item per operator, then sorted by score — not the global
        # top six. Ranking every operator's items together returns six
        # near-duplicate reports of whichever operator had the loudest
        # week (the first build returned the same Grameenphone launch
        # six times from six outlets), which tells a reader nothing about
        # the other four. A board about peers has to show peers.
        initiatives: list[evidence_service.EvidenceItem] = []
        seen_urls: set[str] = set()
        for entity_where, entity_params in per_operator:
            top = evidence_service.critical_items(
                conn, entity_where, entity_params, since=win.since_effective, limit=1, min_score=4.0
            )
            for item in top:
                if item.title.lower() in seen_urls:
                    continue
                seen_urls.add(item.title.lower())
                initiatives.append(item)
        initiatives.sort(key=lambda i: (-i.score, -i.day.toordinal()))

        groups = [{"key": g.key, "title": g.title} for g in watchlist.WATCH_GROUPS]
        custom_ids = {(c.group_key, c.key): c.id for c in watchlist_admin.list_custom(conn)}
        entities = [
            WatchEntityOut(
                id=custom_ids.get((g.key, e.key)),
                groupKey=g.key,
                key=e.key,
                label=e.label,
                note=e.note,
                custom=(g.key, e.key) in custom_ids,
            )
            for g, e in watchlist.all_entities_with_custom(conn)
        ]

    return TelecomTabOut(
        peers=peers,
        wallets=wallets,
        regulatory=regulatory,
        initiatives=_evidence_out(initiatives),
        watchlist=WatchlistOut(groups=groups, entities=entities),
    )


# ── Watchlist add-flow ───────────────────────────────────────────────


@router.post("/watchlist/preview", response_model=WatchPreviewOut)
def watchlist_preview(
    body: WatchPreviewBody, admin: AuthenticatedUser = Depends(require_admin)
) -> WatchPreviewOut:
    """Run proposed terms against the real archive before anything saves.

    This is the guard rail that makes self-service acceptable at all —
    see `app/db/watchlist.py`, which documents why an unchecked pattern
    is dangerous here specifically: Bangla word boundaries do not behave
    like Latin ones, and the bare token রবি matched 222 articles of which
    two were the operator.

    The guard is CHOSEN FOR the operator from the board they picked,
    rather than asked for. Someone adding a wallet should not have to
    know a guard vocabulary exists to find out that নগদ also means cash —
    the screen shows them the difference and lets them decide.
    """
    guard_key = body.context or _DEFAULT_GUARD_FOR_GROUP.get(body.groupKey or "")
    try:
        with get_connection() as conn:
            res = watchlist_admin.preview(
                conn, en_terms=body.enTerms, bn_terms=body.bnTerms, context=guard_key
            )
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    def out(rows: list[watchlist_admin.PreviewSample]) -> list[WatchPreviewSampleOut]:
        return [
            WatchPreviewSampleOut(title=r.title, source=r.source, day=r.day.isoformat(), score=r.score)
            for r in rows
        ]

    guard = watchlist_admin.GUARDS.get(guard_key or "")
    if res.suggest_guard and res.guarded_total is not None:
        message = (
            f"These terms match {res.unguarded_total} headlines on their own. Requiring a "
            f"{guard.label.lower() if guard else 'context'} word alongside them cuts that to "
            f"{res.guarded_total}. Read both lists before you decide."
        )
    elif res.guarded_total is not None:
        message = (
            f"These terms match {res.unguarded_total} headlines, and {res.guarded_total} of those also carry a "
            f"{guard.label.lower() if guard else 'context'} cue. They look specific enough to add as they are."
        )
    else:
        message = (
            f"These terms match {res.unguarded_total} headlines. No guard applies to this board, so read the "
            "sample before adding."
        )

    return WatchPreviewOut(
        unguardedTotal=res.unguarded_total,
        guardedTotal=res.guarded_total,
        unguardedSample=out(res.unguarded_sample),
        guardedSample=out(res.guarded_sample),
        suggestGuard=res.suggest_guard,
        message=message,
        guard=GuardOut(key=guard.key, label=guard.label, blurb=guard.blurb) if guard else None,
    )


@router.post("/watchlist", response_model=WatchEntityOut, status_code=201)
def watchlist_create(body: WatchCreateBody, admin: AuthenticatedUser = Depends(require_admin)) -> WatchEntityOut:
    try:
        with get_connection() as conn:
            created = watchlist_admin.create(
                conn,
                group_key=body.groupKey,
                label=body.label,
                note=body.note,
                en_terms=body.enTerms,
                bn_terms=body.bnTerms,
                context=body.context,
                created_by=admin.email,
            )
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return WatchEntityOut(
        id=created.id,
        groupKey=created.group_key,
        key=created.key,
        label=created.label,
        note=created.note,
        custom=True,
    )


@router.delete("/watchlist/{entity_id}", status_code=204)
def watchlist_delete(
    entity_id: int = Path(..., ge=1), admin: AuthenticatedUser = Depends(require_admin)
) -> None:
    with get_connection() as conn:
        if not watchlist_admin.delete(conn, entity_id):
            raise HTTPException(status_code=404, detail="No such custom watchlist entity.")
