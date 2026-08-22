"""The five named lines on the Long-term dashboard's landing tab.

WHAT A RISK LINE IS
───────────────────
Every other number on this screen is a measurement of one thing: a
count of matched headlines, a mean severity, a closing price. A risk
line is the only composite on the screen — it answers "did this
exposure get worse" by combining whatever evidence actually exists for
that exposure, which is a different source per line:

  regulatory   the regulatory watch board (news-derived)
  competitive  the exchange (market)
  fx           a live spot rate against an annual reference (macro)
  energy       a live commodity price plus the energy news axis
  political    the Political section's matched volume (news-derived)

Because the sources differ, the WINDOWS differ, and every line carries
its own. Market lines run a genuine twelve months and do not thin out
when the collector misses a day; news-derived lines reach only as far
as collection did. A single global caveat would be a lie about at least
two of the five.

DEGRADING HONESTLY
──────────────────
Every input here can be absent: an external API can be down, the
exchange fetch may not have run, a month may have no collection in it.
None of that is an error — it is the normal state of a system that
depends on the outside world. A line with no reading returns
direction "none", value "—" and an evidence sentence saying what is
missing. It never returns 0.0, and it never draws a flat series
through zeroes, because a confident flat line is a picture of data
that was never collected. This is the same rule
`app/api/routers/dashboard.py` already applies to the macro trend
cards, and the reason it exists is a real defect: an unpopulated
indicator once rendered "Inflation 0.0%" under the caption "showing the
last known figure", when there was no last known figure.

COMPARISONS ARE PER COLLECTION DAY
──────────────────────────────────
The collector ran nine days in one month and five in the next. A raw
month-over-month count therefore compares two different amounts of
looking, and would report a fall in risk that is really a fall in
attention. News-derived lines compare RATES per collection day, and
`sub` states the arithmetic in words so a reader can check the arrow
without leaving the screen.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from app.db import econaxes, watchlist
from app.db.connection import DictConnection
from app.logging import get_logger
from app.services import evidence as evidence_service
from app.services import macro as macro_service
from app.services import market as market_service

logger = get_logger(__name__)


def _twelve_months_back(today: date | None = None) -> date:
    """First day of the month eleven months before this one.

    Market lines own this window rather than inheriting the caller's,
    because the exchange does not stop publishing when the collector
    stops running.

    Dhaka, not server-local: every other "what is today" in this codebase
    is pinned to Asia/Dhaka (UTC+6, no DST), and a server running in UTC
    would otherwise shift this window boundary by a whole month during
    the hours either side of a Dhaka midnight.
    """
    d = today or (datetime.now(timezone.utc) + timedelta(hours=6)).date()
    total = d.year * 12 + (d.month - 1) - 11
    return date(total // 12, total % 12 + 1, 1)

@dataclass(frozen=True)
class RiskLine:
    key: str
    label: str
    direction: str
    direction_label: str
    value: str
    unit: str
    sub: str
    evidence: str
    window: str
    window_kind: str
    tone: str
    spark: tuple[float, ...] = ()


def _no_read(key: str, label: str, why: str, window: str, window_kind: str) -> RiskLine:
    """A line we genuinely cannot read right now.

    Deliberately not a zero: "0 high-severity items" is a measurement
    and reads as calm, while "—" reads as absent, which is what it is.
    """
    return RiskLine(
        key=key,
        label=label,
        direction="none",
        direction_label="No read",
        value="—",
        unit="",
        sub=why,
        evidence=why,
        window=window,
        window_kind=window_kind,
        tone="none",
    )


def _normalise(values: list[float]) -> tuple[float, ...]:
    """Scale a COUNT series into 0..1 against its own maximum.

    Zero is meaningful for a count — a month with no items should draw
    as nothing — so the floor is a true zero rather than the series
    minimum. An all-zero or single-point series returns empty instead of
    a flat line, so the card falls back to its hatched rule.
    """
    finite = [v for v in values if v is not None]
    if len(finite) < 2:
        return ()
    top = max(finite)
    if top <= 0:
        return ()
    return tuple(max(0.0, v) / top for v in finite)


def _normalise_range(values: list[float]) -> tuple[float, ...]:
    """Scale an INDEX series into 0..1 against its own min and max.

    An index rebased to 100 that falls to 82 spans 0.82..1.0 of its
    maximum, which draws as twelve bars of near-identical height — a
    picture of a flat year, when the year was in fact a 17% decline.
    Zero is not a meaningful floor for an index, so the floor is the
    series minimum and the shape becomes readable. A series with no
    spread at all returns empty rather than a row of full bars.
    """
    finite = [v for v in values if v is not None]
    if len(finite) < 2:
        return ()
    lo, hi = min(finite), max(finite)
    if hi - lo <= 0:
        return ()
    # A floor of 0.08 keeps the lowest bar visible as a bar rather than
    # vanishing into the card background and reading as missing data.
    return tuple(0.08 + 0.92 * ((v - lo) / (hi - lo)) for v in finite)


# ── individual lines ─────────────────────────────────────────────────


def _regulatory_board(
    conn: DictConnection, group: watchlist.WatchGroup, months: list[date], since: date
) -> list[tuple[str, int, int, float | None]]:
    """(label, critical, scored, mean) for every entity on a board.

    ONE query against `rollup_monthly_watch` rather than one regex scan
    of live `articles` per entity. Both correctness and cost: the rollup
    counts outlive `raw_retention_days`, and seven regex scans over the
    whole article table took ~3s of a single dashboard request.

    Falls back to live stats for the whole board when the rollup has no
    rows for this window at all, which is the state right after an
    entity is added and before the next rollup runs.
    """
    rows = conn.execute(
        "SELECT entity, sum(article_count) AS items, sum(scored_count) AS scored, "
        "sum(high_count) AS critical, sum(score_sum) AS score_sum "
        "FROM rollup_monthly_watch WHERE group_key = %s AND month = ANY(%s) GROUP BY entity",
        (group.key, months),
    ).fetchall()
    by_entity = {r["entity"]: r for r in rows}

    out: list[tuple[str, int, int, float | None]] = []
    for entity in group.entities:
        row = by_entity.get(entity.key)
        if row is not None and (int(row["items"]) or int(row["scored"])):
            scored = int(row["scored"])
            mean = (float(row["score_sum"]) / scored) if scored else None
            out.append((entity.label, int(row["critical"]), scored, mean))
            continue
        where, params = watchlist.entity_where_sql(entity)
        stats = evidence_service.board_stats(conn, where, params, since=since)
        out.append((entity.label, stats.critical, stats.scored, stats.mean))
    return out


def _regulatory_line(
    conn: DictConnection, since: date | None, window: str, monitored: list[date]
) -> RiskLine:
    """Critical regulatory items across the whole regulatory board."""
    if since is None:
        return _no_read(
            "regulatory", "Regulatory & licence", "No month in this window was monitored.", window, "news"
        )
    group = next((g for g in watchlist.WATCH_GROUPS if g.key == "regulatory"), None)
    if group is None:  # pragma: no cover - only reachable if the module is gutted
        return _no_read("regulatory", "Regulatory & licence", "The regulatory board is not configured.", window, "news")

    board = _regulatory_board(conn, group, monitored, since)
    total_critical = sum(c for _, c, _, _ in board)
    total_scored = sum(sc for _, _, sc, _ in board)
    per_entity: list[tuple[str, int, float | None]] = [(lbl, c, m) for lbl, c, _, m in board]

    if total_scored == 0:
        return _no_read(
            "regulatory",
            "Regulatory & licence",
            "Nothing on the regulatory board has been scored in this window yet.",
            window,
            "news",
        )

    scored_entities = [(lbl, c, m) for lbl, c, m in per_entity if m is not None]
    sharpest = max(scored_entities, key=lambda r: r[2] or 0.0) if scored_entities else None
    busiest = max(per_entity, key=lambda r: r[1])

    evidence = (
        f"{busiest[0]} carries the most critical items ({busiest[1]})."
        if busiest[1]
        else "No single axis dominates this window."
    )
    if sharpest is not None and sharpest[2] is not None:
        evidence += f" {sharpest[0]} scores highest at {sharpest[2]:.1f}."

    tone = "bad" if total_critical else "warn"
    return RiskLine(
        key="regulatory",
        label="Regulatory & licence",
        direction="rising" if total_critical else "flat",
        direction_label="Rising" if total_critical else "Quiet",
        value=str(total_critical),
        unit="critical",
        sub=f"of {total_scored} scored items across {len(group.entities)} register axes",
        evidence=evidence,
        window=window,
        window_kind="news",
        tone=tone,
        spark=_normalise([float(c) for _, c, _ in per_entity]),
    )


def _competitive_line(conn: DictConnection) -> RiskLine:
    """Us against the listed peer set, indexed from the window's start.

    Both instruments are rebased to 100 so a stock trading at 247 taka
    and one at 31 can be read on one axis; the line reports the GAP,
    because a CRO's question is not "did we fall" but "did we fall
    relative to the people the regulator treats the same way".

    This line computes its OWN twelve-month window and deliberately
    ignores the news window the caller uses for everything else. Handing
    it the news window was a real defect: with collection running for
    two months, it read two closes instead of twelve and reported a gap
    of −2.5 points where the true twelve-month gap was −22.9. The
    exchange keeps publishing when the collector does not, and this line
    exists to say so.
    """
    window = "Market · 12 months"
    since = _twelve_months_back()
    us = market_service.monthly_closes(conn, "GP", since)
    peer = market_service.monthly_closes(conn, "ROBI", since)
    if len(us) < 2 or len(peer) < 2:
        return _no_read(
            "competitive",
            "Competitive position",
            "Not enough stored exchange history to compare yet.",
            window,
            "market",
        )

    def indexed(rows: list[market_service.MonthClose]) -> list[float]:
        base = rows[0].close
        if base <= 0:
            return []
        return [r.close / base * 100.0 for r in rows]

    us_idx, peer_idx = indexed(us), indexed(peer)
    if not us_idx or not peer_idx:
        return _no_read("competitive", "Competitive position", "Stored closes are unusable.", window, "market")

    gap = us_idx[-1] - peer_idx[-1]
    us_pc = us_idx[-1] - 100.0
    peer_pc = peer_idx[-1] - 100.0
    widened = abs(gap) > abs(us_idx[0] - peer_idx[0])

    behind = gap < 0
    return RiskLine(
        key="competitive",
        label="Competitive position",
        direction="weakening" if behind and widened else "flat" if abs(gap) < 1 else "easing",
        direction_label="Weakening" if behind and widened else "Steady" if abs(gap) < 1 else "Recovering",
        value=f"{gap:+.1f}".replace("+", "+").replace("-", "−"),
        unit="pts",
        sub=f"against the listed peer, indexed to 100 at {us[0].month.strftime('%-d %b %Y')}",
        evidence=(
            f"We are {us_pc:+.1f}% over the window against the peer's {peer_pc:+.1f}%."
        ).replace("-", "−"),
        window=window,
        window_kind="market",
        tone="bad" if behind else "ok",
        spark=_normalise_range(us_idx),
    )


def _fx_line(conn: DictConnection) -> RiskLine:
    """A spot rate with no monthly series behind it, said plainly."""
    window = "Spot"
    live = macro_service.fetch_usd_bdt()
    if live is None:
        return _no_read(
            "fx",
            "FX & capital cost",
            "The exchange-rate source could not be reached, and no rate is stored.",
            window,
            "spot",
        )
    window = f"Spot · {live.fetched_at.date().isoformat()}"
    annual = macro_service.fetch_annual_exchange_rate()
    if annual is None:
        sub = "no annual reference available to compare against"
        direction, label, tone = "flat", "Level only", "warn"
    else:
        delta = (live.value - annual.value) / annual.value * 100.0
        sub = f"against the {annual.year} annual average of {annual.value:.2f}"
        if abs(delta) < 1.0:
            direction, label, tone = "flat", "Flat YoY", "warn"
        elif delta > 0:
            direction, label, tone = "rising", "Weakening", "bad"
        else:
            direction, label, tone = "easing", "Strengthening", "ok"

    return RiskLine(
        key="fx",
        label="FX & capital cost",
        direction=direction,
        direction_label=label,
        value=f"{live.value:.2f}",
        unit="৳/USD",
        sub=sub,
        evidence=(
            "Spot only. No free source publishes a monthly BDT series, so this line is a level, "
            "not a trajectory."
        ),
        window=window,
        window_kind="spot",
        tone=tone,
    )


def _energy_line(conn: DictConnection, since: date | None, window_news: str, brent_key: str) -> RiskLine:
    """Brent spot, with the energy news axis as its supporting evidence."""
    live = macro_service.fetch_brent(brent_key) if brent_key else None
    axis = next((a for a in econaxes.ECON_AXES if a.key == "energy"), None)
    axis_note = ""
    if axis is not None and since is not None:
        where, params = econaxes.axis_where_sql(axis)
        stats = evidence_service.board_stats(conn, where, params, since=since)
        if stats.items:
            axis_note = (
                f" {stats.critical} of {stats.items} energy items in the news window score 8 or above."
            )

    if live is None:
        why = (
            "No Brent price is configured or reachable."
            if not brent_key
            else "The Brent source could not be reached."
        )
        line = _no_read("energy", "Energy & operating cost", why + axis_note, "Spot", "spot")
        return line

    return RiskLine(
        key="energy",
        label="Energy & operating cost",
        direction="flat",
        direction_label="No history",
        value=f"{live.value:.2f}",
        unit="USD/bbl",
        sub="Brent spot — tower diesel and grid exposure",
        evidence=("A level, not a trajectory: no series is stored behind it." + axis_note).strip(),
        window=f"Spot · {live.fetched_at.date().isoformat()}",
        window_kind="spot",
        tone="warn",
    )


def _political_line(
    conn: DictConnection,
    monitored: list[date],
    coverage_days: dict[date, int],
    window: str,
) -> RiskLine:
    """Political-section volume, compared per collection day."""
    if not monitored:
        return _no_read("political", "Political stability", "No month in this window was monitored.", window, "news")

    rows = conn.execute(
        "SELECT month, matched_count FROM rollup_monthly_section "
        "WHERE section = %s AND month = ANY(%s) ORDER BY month",
        ("Political", monitored),
    ).fetchall()
    by_month = {r["month"]: int(r["matched_count"]) for r in rows}
    latest = monitored[-1]
    latest_n = by_month.get(latest, 0)

    if latest_n == 0 and len(by_month) == 0:
        return _no_read(
            "political",
            "Political stability",
            "No Political-section coverage was rolled up for this window.",
            window,
            "news",
        )

    latest_days = max(1, coverage_days.get(latest, 0))
    latest_rate = latest_n / latest_days

    direction, label, tone = "flat", "Steady", "warn"
    sub = f"matched items in {latest.strftime('%b %Y')}"
    if len(monitored) > 1:
        prev = monitored[-2]
        prev_days = max(1, coverage_days.get(prev, 0))
        prev_rate = by_month.get(prev, 0) / prev_days
        if prev_rate > 0:
            change = (latest_rate - prev_rate) / prev_rate * 100.0
            sub = (
                f"{latest_rate:.1f} per collection day in {latest.strftime('%b')} "
                f"against {prev_rate:.1f} in {prev.strftime('%b')}"
            )
            if change > 10:
                direction, label, tone = "elevated", "Elevated", "bad"
            elif change < -10:
                direction, label, tone = "easing", "Easing", "ok"

    return RiskLine(
        key="political",
        label="Political stability",
        direction=direction,
        direction_label=label,
        value=f"{latest_n:,}",
        unit="items",
        sub=sub,
        evidence=(
            "Compared per collection day, not per month: the collector ran a different number of days in each."
        ),
        window=window,
        window_kind="news",
        tone=tone,
        spark=_normalise([float(by_month.get(m, 0)) for m in monitored]),
    )


# ── public ───────────────────────────────────────────────────────────


def build_risk_lines(
    conn: DictConnection,
    *,
    since: date | None,
    monitored: list[date],
    coverage_days: dict[date, int],
    news_window: str,
    brent_key: str,
) -> list[RiskLine]:
    """All five lines. One failing line never takes the others down."""
    builders = (
        ("regulatory", lambda: _regulatory_line(conn, since, news_window, monitored)),
        ("competitive", lambda: _competitive_line(conn)),
        ("fx", lambda: _fx_line(conn)),
        ("energy", lambda: _energy_line(conn, since, news_window, brent_key)),
        ("political", lambda: _political_line(conn, monitored, coverage_days, news_window)),
    )
    out: list[RiskLine] = []
    for key, build in builders:
        try:
            out.append(build())
        except Exception:
            logger.warning("risklines.build: %s failed, showing it as unreadable", key, exc_info=True)
            out.append(_no_read(key, key.title(), "This line could not be computed.", news_window, "news"))
    return out
