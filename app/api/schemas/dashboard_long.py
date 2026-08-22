"""Response models for the rebuilt Long-term dashboard — the four
sub-tabs under Dashboard > Long term (Risk lines, Market, Economy and
companies, Telecom watch).

WHY A SEPARATE MODULE FROM core.py
──────────────────────────────────
`DashboardTrendsOut` in app.api.schemas.core is the OLD long-term
payload: one object carrying trend cards, a 12-month section stack, a
theme heatmap, standing themes and the two watch boards, all read in a
single request. The rebuilt screen is four tabs that are read one at a
time, and three of them are backed by data the old payload never
carried (real DSE market history, economy axes, and the individual
headlines behind every count). Bolting that onto `DashboardTrendsOut`
would make every tab pay for all four.

So: one endpoint per tab, one model per endpoint, in their own module.
`DashboardTrendsOut` and `GET /dashboard/trends` are left exactly as
they were — the Short-term tab and the existing tests still use them.

THE TWO WINDOWS
───────────────
Every number on this screen belongs to one of two spans, and the two
are not interchangeable:

  * MARKET and MACRO run a genuine twelve months. They come from the
    exchange and from macro APIs, so they do not thin out when the
    collector misses a day.
  * NEWS-DERIVED numbers only reach as far as collection did.

Mixing them silently is how a board paper ends up citing a two-week
sample as a year. Every board and card therefore carries its own
`window` string and a `windowKind` naming which span it belongs to,
rather than the screen carrying one global caveat.
"""

from __future__ import annotations

from pydantic import BaseModel

# ── Evidence ─────────────────────────────────────────────────────────


class EvidenceOut(BaseModel):
    """One article standing behind a number on a board.

    `reason` is `articles.llm_reason` VERBATIM — the justification the
    model recorded when it scored the item. It is never regenerated for
    display and never paraphrased: the whole point of showing it is that
    a risk officer can check the score against the stated reason, and a
    freshly-written sentence would be a different claim wearing the same
    badge. An item scored before the reason column was populated returns
    an empty string, and the screen shows that absence rather than
    inventing prose to fill it.

    `band` is the rubric's own band (app/services/llm/prompts.py), not a
    display invention, so a chip here means what it means in the review
    queue.
    """

    articleId: int
    score: float
    band: str
    title: str
    source: str
    day: str
    reason: str


# ── Boards (shared by Telecom watch and Economy) ─────────────────────


class BoardRowOut(BaseModel):
    """One row of a board: a watchlist entity or an economy axis.

    `mean` is None when nothing in the row has been scored yet — which
    is a real state, not an error. Airtel arrived on the operator board
    with sixteen matched items and none scored, and a `mean` of 0.0
    would have read as "measured, and calm". The screen draws a hatched
    rule instead of a bar for these.

    `criticalTruncated` reports whether `crit` is the whole set or the
    head of it, so the screen can say "3 of 172 shown" rather than
    implying it has listed everything.
    """

    key: str
    name: str
    note: str
    items: int
    scored: int
    critical: int
    mean: float | None = None
    # Marks the row as us rather than a peer. The operator board is a
    # share-of-voice board, not a competitor list, so Grameenphone is on
    # it on purpose and has to be visually distinguishable.
    isUs: bool = False
    # Recently added to the watchlist and not yet part of any historical
    # comparison. Worth a mark so a reader does not read a short history
    # as a quiet one.
    isNew: bool = False
    crit: list[EvidenceOut] = []
    criticalTruncated: bool = False
    # True when the row has critical items on the books but the articles
    # themselves have aged past the raw-retention window, so the
    # headlines can no longer be shown. Distinct from "no critical
    # items": one is nothing to see, the other is something we can no
    # longer show you.
    critExpired: bool = False


class BoardOut(BaseModel):
    key: str
    title: str
    blurb: str
    window: str
    windowKind: str
    rows: list[BoardRowOut]
    verdict: str = ""


# ── Risk lines (the landing tab) ─────────────────────────────────────


class RiskLineOut(BaseModel):
    """One named line on the register.

    These are the only composite figures on the screen: each is derived
    from a different underlying source (a watch board, the exchange, a
    macro API, a section count), which is exactly why each carries its
    own `window` rather than inheriting one.

    `direction` is a machine token; `directionLabel` is the word the
    screen prints. Both exist because the word carries the meaning and
    the arrow only reinforces it — a rising arrow on a risk line is bad
    news, so direction is never left to colour or glyph alone.

    `value` is a preformatted string, not a number. The five lines are
    measured in different units (a count, an index gap in points, a
    currency rate, a dollar price) with different decimal conventions,
    and formatting them once here keeps the screen from having to know
    which is which.
    """

    key: str
    label: str
    direction: str
    directionLabel: str
    value: str
    unit: str
    sub: str
    evidence: str
    window: str
    windowKind: str
    tone: str
    # A short series for the card's inline strip, already normalised to
    # 0..1. Empty when the line has a reading but no history behind it
    # (FX and Brent both do) — the card then draws a hatched rule, never
    # a flat line through zeroes, because a confident flat line is a
    # picture of data that was never collected.
    spark: list[float] = []


class ProvenanceOut(BaseModel):
    market: str
    news: str
    unavailable: str


class RiskTabOut(BaseModel):
    lines: list[RiskLineOut]
    moved: list[EvidenceOut]
    provenance: ProvenanceOut


# ── Market tab ───────────────────────────────────────────────────────


class InstrumentOut(BaseModel):
    """One listed instrument as the exchange reports it.

    `rangePos` is where `last` sits inside the 52-week range, 0..1. It
    is precomputed because it is the one number on the panel that says
    something a day's percentage move does not: a stock at 0.14 of its
    year range and one at 0.64 are in different situations even when
    both moved a fraction of a percent today.
    """

    code: str
    name: str
    last: float
    changePct: float
    dayLow: float
    dayHigh: float
    w52Low: float
    w52High: float
    marketCapMn: float
    freeFloatMn: float | None = None
    asOf: str
    # Both are None when the 52-week range is degenerate (high == low),
    # which happens for a newly listed or barely traded instrument. 0.0
    # would render as "pinned at its 52-week low" — a claim about the
    # stock rather than an admission that there is no position to report.
    belowHighPct: float | None = None
    rangePos: float | None = None


class IndexSeriesOut(BaseModel):
    code: str
    label: str
    # Rebased to 100 at the first month of the window, so two
    # instruments trading at 247 and 31 taka can be read on one axis.
    values: list[float]


class MonthRowOut(BaseModel):
    label: str
    closes: dict[str, float | None]
    moms: dict[str, float | None]


class MarketTabOut(BaseModel):
    instruments: list[InstrumentOut]
    labels: list[str]
    series: list[IndexSeriesOut]
    months: list[MonthRowOut]
    tradingDays: int
    asOf: str
    window: str
    note: str


# ── Economy tab ──────────────────────────────────────────────────────


class MacroCardOut(BaseModel):
    """One macro indicator.

    `status` is one of live / annual / stale / unconfigured / no_data and
    decides what the card is allowed to claim. `hasSeries` is separate
    from it on purpose: an indicator can be perfectly live and still have
    no history behind it, which is true of both the USD/BDT spot rate and
    Brent here. A live reading with no series is a level, not a
    trajectory, and the card has to say so.
    """

    key: str
    label: str
    value: str
    unit: str
    status: str
    note: str
    asOf: str = ""
    hasSeries: bool = False
    series: list[float] = []


class EconomyTabOut(BaseModel):
    macro: list[MacroCardOut]
    board: BoardOut


# ── Telecom watch tab ────────────────────────────────────────────────


class WatchEntityOut(BaseModel):
    id: int | None = None
    groupKey: str
    key: str
    label: str
    note: str
    # Code-defined entities are reviewed against the corpus before they
    # land and cannot be deleted from the UI; custom ones can. The screen
    # needs to know which is which before it offers a delete control.
    custom: bool = False


class WatchlistOut(BaseModel):
    groups: list[dict[str, str]]
    entities: list[WatchEntityOut]


class TelecomTabOut(BaseModel):
    peers: BoardOut
    wallets: BoardOut
    regulatory: BoardOut
    initiatives: list[EvidenceOut]
    watchlist: WatchlistOut


# ── Watchlist add-flow ───────────────────────────────────────────────


class WatchPreviewSampleOut(BaseModel):
    title: str
    source: str
    day: str
    score: float | None = None


class GuardOut(BaseModel):
    key: str
    label: str
    blurb: str


class WatchPreviewOut(BaseModel):
    """What a proposed set of terms would actually catch.

    This is the safety rail that makes self-service acceptable at all.
    `app.db.watchlist` argues at length that adding an entity is a code
    change precisely because an unchecked pattern re-imports the problem
    the module exists to solve — রবি matched 222 articles of which two
    were the operator. Rather than remove the rail, the add flow runs the
    proposed terms against the real archive and shows both totals before
    anything is saved.

    `guardedTotal` is None when the user proposed no guard. `suggestGuard`
    is the service's judgement that one is needed.
    """

    unguardedTotal: int
    guardedTotal: int | None = None
    unguardedSample: list[WatchPreviewSampleOut]
    guardedSample: list[WatchPreviewSampleOut]
    suggestGuard: bool
    message: str
    # The guard the preview actually ran, so the screen can name it
    # instead of saying "a guard" and can offer it as the save option.
    # None when the chosen board has no guard vocabulary.
    guard: GuardOut | None = None


class WatchPreviewBody(BaseModel):
    enTerms: list[str] = []
    bnTerms: list[str] = []
    # Which board the entity is destined for. The preview uses it to pick
    # the guard worth testing — a wallet term is checked against payments
    # cues, an operator term against telecom cues — so the operator sees
    # the guard's effect without having to know the guard vocabulary
    # exists. `context` still overrides it when set.
    groupKey: str | None = None
    context: str | None = None


class WatchCreateBody(BaseModel):
    groupKey: str
    label: str
    note: str = ""
    enTerms: list[str] = []
    bnTerms: list[str] = []
    context: str | None = None
