"""Dhaka Stock Exchange market data for the dashboard's "Market" tab:
a live quote and a 12-month close series for the two listed mobile
operators this platform's reader carries risk on -- Grameenphone
(trading code ``GP``, scrip 27001) and Robi Axiata (``ROBI``, 27003).

Source, and why it is a scrape
------------------------------
Everything here comes from **dsebd.org**, the exchange's own website:

  * Quote    -- https://dsebd.org/displayCompany.php?name=GP
                The "Market Information" table: Last Trading Price,
                Day's Range, 52 Weeks' Moving Range, Change*, Market
                Capitalization (mn), Free Float Market Cap. (mn), plus
                the session date the exchange prints in the heading.
  * History  -- https://dsebd.org/day_end_archive.php?startDate=..&endDate=..&inst=GP&archive=data
                The day-end archive table; column 1 is DATE (already
                ``YYYY-MM-DD``) and column 7 is CLOSEP*, the close.

The DSE publishes no API, free or paid, and no CSV or JSON endpoint --
the HTML tables above are the only public form this data exists in. So
unlike `app.services.macro`, whose three indicators all come from real
JSON APIs, this module parses markup, which is a materially more
fragile contract: a template change at DSE breaks it. That is exactly
why the parsing is defensive (label-driven, not positional, for the
quote page; shape-validated per row for the archive) and why every
parse failure resolves to None / an empty list with a WARNING rather
than a partially-filled record. A wrong number on a risk dashboard is
worse than a missing one.

No ``bs4``/``lxml``: neither is a dependency of this project (same
finding as `app.sources._util`), so the parser below is stdlib
``html.parser``.

TLS -- the one thing worth reading before touching this file
------------------------------------------------------------
**dsebd.org serves an INCOMPLETE certificate chain.** It sends its leaf
(``CN=*.dsebd.org``) and the Sectigo root, but omits the intermediate
that actually signs the leaf, so no client can build a path: ``curl``
fails with "unable to get local issuer certificate" and ``requests``
raises ``SSLError``. That is a real, reproducible defect on their side,
not a transient error.

The fix here is NOT to disable verification. ``verify=False`` would
turn every request to a Bangladeshi financial site into something any
network position could rewrite, and the numbers it returns are shown to
a Chief Risk Officer as fact. Instead this module supplies the missing
link and keeps verification fully on:

    ``verify=CA_BUNDLE_PATH`` -> app/services/dsebd_ca_bundle.crt

That file contains two certificates and nothing else:

  1. ``Sectigo Public Server Authentication CA DV R36`` -- the
     intermediate dsebd.org fails to send. Obtained from the leaf's own
     AIA extension (http://crt.sectigo.com/SectigoPublicServerAuthenticationCADVR36.crt)
     and then *verified* with ``openssl verify`` against (2), rather
     than trusted because it downloaded.
  2. ``Sectigo Public Server Authentication Root R46`` -- the trust
     anchor, extracted verbatim from the ``certifi`` package already
     installed here, i.e. the Mozilla CA program's own copy.

So trusting this bundle for this host is exactly as strong as trusting
certifi for it: the same public root, reached through one intermediate
whose signature by that root was checked. Full path validation --
signatures, validity dates, hostname -- still happens on every request;
nothing is bypassed and no certificate is silently accepted. The bundle
scopes trust to that one chain, which is if anything narrower than the
system store.

The file is PEM-encoded but named ``.crt``, not ``.pem``, for one
mundane reason: `.gitignore` ignores ``*.pem`` under its "Secrets"
heading (alongside ``*.key``), and this bundle MUST be committed or a
deployed instance fails on a missing file. It holds public certificates
only -- no private key -- so it is not a secret; ``.crt`` for a
PEM-encoded bundle is also what Debian itself uses
(``/etc/ssl/certs/ca-certificates.crt``).

Certificate (1) expires 2036-03-21. If dsebd.org ever fixes its chain,
delete the bundle and drop the ``verify=`` argument. See the header
comment inside that file for provenance and fingerprints.

Contracts this module keeps
---------------------------
Every ``fetch_*`` function:

  * **NEVER raises into its caller.** Network error, timeout, non-2xx,
    TLS failure, template change, unparseable number -- all are caught,
    logged at WARNING, and turned into ``None`` / ``[]``. Same mandate
    as `app.services.macro`.
  * Is cached in-process for a TTL matched to how often the underlying
    data can actually change (a plain module-level dict; no DB table, no
    new dependency), so a dashboard with several viewers does not
    re-scrape dsebd.org per request. Failures are never cached, so a
    transient outage does not pin an empty chart in place for hours.

The ``store_*`` / ``latest_quote`` / ``monthly_closes`` functions are
the DB half (migrations/0024_market_prices.sql) and DO raise -- a
database error is our own bug and must not be swallowed. Only the
external world is treated as unreliable.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, TypeVar, cast

import requests

from app.db.connection import DictConnection
from app.logging import get_logger

logger = get_logger(__name__)

REQUEST_TIMEOUT_SECONDS = 20

QUOTE_URL = "https://dsebd.org/displayCompany.php"
ARCHIVE_URL = "https://dsebd.org/day_end_archive.php"

# See this module's "TLS" docstring section. Never replace this with
# verify=False, and never pass verify=<anything> globally -- the bundle
# is scoped to dsebd.org requests made from this module.
CA_BUNDLE_PATH = str(Path(__file__).resolve().parent / "dsebd_ca_bundle.crt")

# dsebd.org returns an HTML error page rather than a 4xx for a plain
# unrecognised client, so a browser-ish UA is not optional politeness
# here -- it is what makes the response parseable.
USER_AGENT = "Mozilla/5.0 (compatible; ERM-News-Intelligence/1.0; +https://dsebd.org)"

# Cache TTLs. The quote page carries an intraday "Last Update" time but
# DSE trading is a few hours a day and this dashboard is not a trading
# terminal -- 15 minutes is far fresher than any decision made off this
# screen needs, and keeps our request rate to dsebd.org trivial. The
# day-end archive only changes once per day after the close.
QUOTE_TTL_SECONDS = 15 * 60
HISTORY_TTL_SECONDS = 6 * 3600

# One year of daily closes is what the tab draws; refresh() uses this.
HISTORY_WINDOW_DAYS = 366


@dataclass(frozen=True)
class Quote:
    """One scraped snapshot of a company's DSE quote page.

    `as_of` is the session date DSE itself printed on the page, NOT the
    time we fetched it -- on a weekend or holiday the site keeps serving
    the last trading session, and the UI must be able to say which
    session the numbers belong to.
    """

    code: str
    name: str
    last: float
    change_pct: float
    day_low: float
    day_high: float
    week52_low: float
    week52_high: float
    market_cap_mn: float
    free_float_mn: float | None
    as_of: date


@dataclass(frozen=True)
class MonthClose:
    """The last close that actually traded in one calendar month.

    `month` is the first of that month, matching the `month DATE` key
    convention every rollup_monthly_* table in this schema already uses.
    A month with no trading days produces no MonthClose at all -- see
    `monthly_closes`.
    """

    month: date
    close: float


# ── in-process cache ─────────────────────────────────────────────────

T = TypeVar("T")

# key -> (value, monotonic-clock expiry). Process-wide and deliberately
# simple, exactly as in app/services/macro.py: a handful of keys with
# minute-to-hour TTLs does not justify a cache dependency or a table.
_cache: dict[str, tuple[Any, float]] = {}


def _cached(key: str, ttl_seconds: float, do_fetch: Callable[[], T | None]) -> T | None:
    """Serve `key` from the cache when live, else fetch and store.

    Only TRUTHY results are cached. That is the important half: a None
    quote or an empty history means the fetch failed, and caching a
    failure would keep the tab blank for the whole TTL after a
    five-second blip at DSE.
    """
    now = time.monotonic()
    hit = _cache.get(key)
    if hit is not None and hit[1] > now:
        return cast("T", hit[0])
    value = do_fetch()
    if value:
        _cache[key] = (value, now + ttl_seconds)
    return value


def _clear_cache_for_tests() -> None:
    """Test-only escape hatch (tests/test_market.py) -- clears the
    module-level cache so one test's monkeypatched `requests.get` cannot
    leak a cached value into the next.
    """
    _cache.clear()


# ── HTML parsing (stdlib only) ───────────────────────────────────────


class _TableTextParser(HTMLParser):
    """Flattens a DSE page into the two shapes this module needs.

    `cells` is every ``<h2>``/``<th>``/``<td>`` in document order as
    ``(tag, text)`` -- what the quote page is read from, by LABEL rather
    than by position, so DSE reordering rows does not silently shift
    which number lands in which field.

    `rows` groups ``<td>``/``<th>`` texts per ``<tr>`` -- what the
    day-end archive table is read from.

    Nested tables are handled with a stack, because the quote page nests
    one inside the ``Change*`` cell: the inner cells are emitted first
    (giving the change value and the change percent as two consecutive
    entries in `cells`) and the outer wrapper emits empty text, which
    `_value_after` skips.
    """

    _CAPTURE = frozenset({"h2", "th", "td"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.cells: list[tuple[str, str]] = []
        self.rows: list[list[str]] = []
        self._stack: list[tuple[str, list[str]]] = []
        self._row: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "tr":
            self._row = []
        elif tag in self._CAPTURE:
            self._stack.append((tag, []))

    def handle_data(self, data: str) -> None:
        if self._stack:
            self._stack[-1][1].append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "tr":
            if self._row is not None and len(self._row) > 1:
                self.rows.append(self._row)
            self._row = None
            return
        if tag not in self._CAPTURE or not self._stack or self._stack[-1][0] != tag:
            # Unbalanced markup: drop it rather than guessing. DSE's
            # pages are well-formed enough that this does not fire, and
            # guessing is how a parser starts inventing values.
            return
        _open_tag, parts = self._stack.pop()
        text = _squash("".join(parts))
        self.cells.append((tag, text))
        if self._row is not None:
            self._row.append(text)


def _squash(text: str) -> str:
    """Collapse all whitespace (the pages are tab-indented) and strip."""
    return re.sub(r"\s+", " ", text).strip()


def _value_after(cells: list[tuple[str, str]], label: str, skip: int = 0) -> str | None:
    """Text of the (`skip`+1)-th non-empty cell following `label`.

    `skip` exists for the ``Change*`` row, whose nested table yields the
    absolute change and then the percent change as two cells.
    """
    target = label.casefold()
    for index, (_tag, text) in enumerate(cells):
        if text.casefold() != target:
            continue
        seen = 0
        for _next_tag, value in cells[index + 1 :]:
            if not value:
                continue
            if seen == skip:
                return value
            seen += 1
        return None
    return None


def _heading_value(cells: list[tuple[str, str]], prefix: str) -> str | None:
    """Text after `prefix` in the first ``<h2>`` that starts with it.

    The page's two non-table facts live in headings: "Company Name:
    Grameenphone Ltd." and "Market Information: Aug 20, 2026".
    """
    lowered = prefix.casefold()
    for tag, text in cells:
        if tag == "h2" and text.casefold().startswith(lowered):
            return text[len(prefix) :].strip()
    return None


def _to_float(raw: str | None) -> float | None:
    """Parse one DSE number: thousands separators and a trailing % are
    stripped. Returns None -- never 0.0 -- for anything unparseable, so
    a template change can't be mistaken for a real zero price.
    """
    if raw is None:
        return None
    cleaned = raw.replace(",", "").replace("%", "").strip()
    if not re.fullmatch(r"[+-]?\d*\.?\d+", cleaned):
        return None
    return float(cleaned)


def _to_range(raw: str | None) -> tuple[float, float] | None:
    """Parse a DSE "low - high" range cell, e.g. ``247.20 - 249.80``."""
    if raw is None:
        return None
    match = re.fullmatch(r"\s*([\d,]*\.?\d+)\s*-\s*([\d,]*\.?\d+)\s*", raw)
    if match is None:
        return None
    low = _to_float(match.group(1))
    high = _to_float(match.group(2))
    if low is None or high is None:
        return None
    return low, high


def parse_quote(code: str, html_text: str) -> Quote | None:
    """Parse a displayCompany.php page into a `Quote`.

    Separate from `fetch_quote` so the parser can be tested against
    saved real pages (tests/fixtures_market/) with no network at all.
    Returns None if ANY required field is missing -- a half-parsed quote
    is not published. Never raises.
    """
    try:
        parser = _TableTextParser()
        parser.feed(html_text)

        name = _heading_value(parser.cells, "Company Name:")
        as_of_text = _heading_value(parser.cells, "Market Information:")
        last = _to_float(_value_after(parser.cells, "Last Trading Price"))
        # Change* is a nested table: cell 0 is the absolute move, cell 1
        # the percent. The percent is what the dashboard shows.
        change_pct = _to_float(_value_after(parser.cells, "Change*", skip=1))
        day_range = _to_range(_value_after(parser.cells, "Day's Range"))
        week52_range = _to_range(_value_after(parser.cells, "52 Weeks' Moving Range"))
        market_cap_mn = _to_float(_value_after(parser.cells, "Market Capitalization (mn)"))
        # The only optional number: DSE omits the free float line for
        # some instruments and a quote is still worth showing without it.
        free_float_mn = _to_float(_value_after(parser.cells, "Free Float Market Cap. (mn)"))

        as_of = _parse_as_of(as_of_text)

        if (
            not name
            or as_of is None
            or last is None
            or change_pct is None
            or day_range is None
            or week52_range is None
            or market_cap_mn is None
        ):
            logger.warning(
                "market.parse_quote: page did not yield a complete quote, discarding",
                extra={"code": code},
            )
            return None

        return Quote(
            code=code.upper(),
            name=name,
            last=last,
            change_pct=change_pct,
            day_low=day_range[0],
            day_high=day_range[1],
            week52_low=week52_range[0],
            week52_high=week52_range[1],
            market_cap_mn=market_cap_mn,
            free_float_mn=free_float_mn,
            as_of=as_of,
        )
    except Exception:
        logger.warning("market.parse_quote: unparseable page", extra={"code": code}, exc_info=True)
        return None


def _parse_as_of(text: str | None) -> date | None:
    """DSE prints the session date as ``Aug 20, 2026``."""
    if not text:
        return None
    try:
        return datetime.strptime(text.strip(), "%b %d, %Y").date()
    except ValueError:
        return None


def parse_daily_history(code: str, html_text: str) -> list[tuple[date, float]]:
    """Parse a day_end_archive.php table into ``(day, close)`` pairs,
    ascending by day.

    Rows are validated by SHAPE, not trusted by position alone: a row
    counts only if it has the archive's full column set, its DATE cell
    really is ``YYYY-MM-DD``, its TRADING CODE cell really is the
    instrument asked for, and its CLOSEP* cell really is a number. Rows
    that fail are skipped silently -- the same table carries the header
    row and the site's own navigation tables. If NOTHING survives, that
    is logged, because an empty result and a broken parser look
    identical to the caller otherwise.

    Separate from `fetch_daily_history` so it can be tested against a
    saved real page. Never raises.
    """
    try:
        parser = _TableTextParser()
        parser.feed(html_text)

        wanted = code.strip().upper()
        by_day: dict[date, float] = {}
        for row in parser.rows:
            if len(row) < 8:
                continue
            if row[2].strip().upper() != wanted:
                continue
            try:
                day = date.fromisoformat(row[1].strip())
            except ValueError:
                continue
            close = _to_float(row[7])
            if close is None:
                continue
            # Last write wins on a repeated day; DSE does occasionally
            # restate, and the archive lists newest first, so iterate
            # order alone must not decide.
            by_day[day] = close

        if not by_day:
            logger.warning(
                "market.parse_daily_history: no usable rows in archive page",
                extra={"code": code},
            )
        return sorted(by_day.items())
    except Exception:
        logger.warning("market.parse_daily_history: unparseable page", extra={"code": code}, exc_info=True)
        return []


# ── fetch layer (never raises) ───────────────────────────────────────


def _get(url: str, params: dict[str, str]) -> str | None:
    """One verified GET against dsebd.org, returning the body or None.

    `verify=CA_BUNDLE_PATH` is the whole TLS decision -- see this
    module's docstring. Never raises.
    """
    try:
        resp = requests.get(
            url,
            params=params,
            headers={"User-Agent": USER_AGENT},
            timeout=REQUEST_TIMEOUT_SECONDS,
            verify=CA_BUNDLE_PATH,
        )
        resp.raise_for_status()
        return resp.text
    except Exception:
        logger.warning("market._get: fetch failed", extra={"url": url, "params": params}, exc_info=True)
        return None


def fetch_quote(code: str) -> Quote | None:
    """Live quote for one DSE trading code (e.g. ``GP``). Cached ~15min.
    Returns None on any network, TLS, HTTP or parse failure -- never
    raises.
    """

    def _do_fetch() -> Quote | None:
        body = _get(QUOTE_URL, {"name": code.strip().upper()})
        if body is None:
            return None
        return parse_quote(code, body)

    return _cached(f"quote:{code.strip().upper()}", QUOTE_TTL_SECONDS, _do_fetch)


def fetch_daily_history(code: str, start: date, end: date) -> list[tuple[date, float]]:
    """Day-end closes for one DSE trading code over ``[start, end]``,
    ascending by day. Cached ~6h (the archive changes once a day, after
    the close). Returns ``[]`` on any failure -- never raises.
    """

    def _do_fetch() -> list[tuple[date, float]] | None:
        body = _get(
            ARCHIVE_URL,
            {
                "startDate": start.isoformat(),
                "endDate": end.isoformat(),
                "inst": code.strip().upper(),
                "archive": "data",
            },
        )
        if body is None:
            return None
        return parse_daily_history(code, body)

    key = f"history:{code.strip().upper()}:{start.isoformat()}:{end.isoformat()}"
    return _cached(key, HISTORY_TTL_SECONDS, _do_fetch) or []


# ── storage layer (raises: a DB error is our bug, not the world's) ────


def store_history(conn: DictConnection, code: str, rows: list[tuple[date, float]]) -> int:
    """Upsert ``(day, close)`` pairs into market_daily_close.

    Returns the number of DISTINCT trading days written -- deduplicated
    first, so the count is what the table actually gained/refreshed
    rather than how long the input list happened to be. Idempotent by
    (code, day) with last-write-wins, so re-running an overlapping
    window corrects restated closes instead of duplicating or erroring
    -- the same ON CONFLICT ... DO UPDATE convention as app/db/rollups.py.
    """
    if not rows:
        return 0
    wanted = code.strip().upper()
    by_day: dict[date, float] = dict(rows)
    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO market_daily_close (code, day, close_price)
            VALUES (%s, %s, %s)
            ON CONFLICT (code, day) DO UPDATE SET
                close_price = EXCLUDED.close_price
            """,
            [(wanted, day, close) for day, close in sorted(by_day.items())],
        )
    conn.commit()
    return len(by_day)


def store_quote(conn: DictConnection, q: Quote) -> None:
    """Upsert the latest quote for one code into market_quote.

    One row per code by construction (code is the primary key) --
    market_quote is a snapshot, not a history; the durable series is
    market_daily_close. `fetched_at` is refreshed on every write so the
    UI can distinguish "the market has not moved" from "we have not been
    able to reach DSE since Tuesday".
    """
    conn.execute(
        """
        INSERT INTO market_quote (
            code, name, last_price, change_pct, day_low, day_high,
            week52_low, week52_high, market_cap_mn, free_float_mn, as_of, fetched_at
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
        ON CONFLICT (code) DO UPDATE SET
            name          = EXCLUDED.name,
            last_price    = EXCLUDED.last_price,
            change_pct    = EXCLUDED.change_pct,
            day_low       = EXCLUDED.day_low,
            day_high      = EXCLUDED.day_high,
            week52_low    = EXCLUDED.week52_low,
            week52_high   = EXCLUDED.week52_high,
            market_cap_mn = EXCLUDED.market_cap_mn,
            free_float_mn = EXCLUDED.free_float_mn,
            as_of         = EXCLUDED.as_of,
            fetched_at    = now()
        """,
        (
            q.code.strip().upper(),
            q.name,
            q.last,
            q.change_pct,
            q.day_low,
            q.day_high,
            q.week52_low,
            q.week52_high,
            q.market_cap_mn,
            q.free_float_mn,
            q.as_of,
        ),
    )
    conn.commit()


def latest_quote(conn: DictConnection, code: str) -> Quote | None:
    """The stored quote for one code, or None if we have never
    successfully scraped it. This is what the request path reads -- it
    never triggers a fetch, so a DSE outage shows the last known quote
    (labelled with its own `as_of`) rather than failing the request.
    """
    row = conn.execute(
        """
        SELECT code, name, last_price, change_pct, day_low, day_high,
               week52_low, week52_high, market_cap_mn, free_float_mn, as_of
        FROM market_quote
        WHERE code = %s
        """,
        (code.strip().upper(),),
    ).fetchone()
    if row is None:
        return None
    free_float = row["free_float_mn"]
    return Quote(
        code=row["code"],
        name=row["name"],
        last=float(row["last_price"]),
        change_pct=float(row["change_pct"]),
        day_low=float(row["day_low"]),
        day_high=float(row["day_high"]),
        week52_low=float(row["week52_low"]),
        week52_high=float(row["week52_high"]),
        market_cap_mn=float(row["market_cap_mn"]),
        free_float_mn=None if free_float is None else float(free_float),
        as_of=row["as_of"],
    )


def monthly_closes(conn: DictConnection, code: str, since: date) -> list[MonthClose]:
    """One point per calendar month from `since` onward: the LAST close
    that actually traded in that month, ascending by month.

    A month in which the instrument never traded is ABSENT from the
    result -- it is not zero-filled and not carried forward. Both of
    those would be inventions: zero would draw as a crash, and a
    carried-forward price would assert a value nobody quoted. This is
    the same rule migration 0021's rollup_monthly_coverage exists to
    enforce for article counts, and the caller is expected to render a
    gap as a gap.
    """
    rows = conn.execute(
        """
        SELECT DISTINCT ON (date_trunc('month', day))
               date_trunc('month', day)::date AS month,
               close_price
        FROM market_daily_close
        WHERE code = %s AND day >= %s
        ORDER BY date_trunc('month', day), day DESC
        """,
        (code.strip().upper(), since),
    ).fetchall()
    return [MonthClose(month=row["month"], close=float(row["close_price"])) for row in rows]


def stored_day_count(conn: DictConnection, code: str) -> int:
    """How many trading days are actually stored for `code`.

    The dashboard's provenance line cites this rather than the calendar
    span, because the two are different claims: "twelve months" is a
    window we asked for, and this is what the exchange actually
    published inside it. Citing the window when the store is half empty
    is the same defect as drawing an unmonitored month as zero.
    """
    row = conn.execute(
        "SELECT count(*) AS n FROM market_daily_close WHERE code = %s",
        (code.strip().upper(),),
    ).fetchone()
    return int(row["n"]) if row else 0


def refresh(conn: DictConnection, codes: tuple[str, ...] = ("GP", "ROBI")) -> dict[str, int]:
    """Scrape and store quote + 12 months of closes for each code.

    The background half of this module -- called by the daily batch, NOT
    from a request path. Returns ``{code: daily rows written}``; a code
    whose fetch failed maps to 0 and leaves whatever is already stored
    untouched, which is the point of storing at all (see
    migrations/0024_market_prices.sql). Never raises on a source
    failure; a DB failure still propagates.
    """
    today = date.today()
    start = today - timedelta(days=HISTORY_WINDOW_DAYS)
    written: dict[str, int] = {}
    for code in codes:
        rows = fetch_daily_history(code, start, today)
        written[code] = store_history(conn, code, rows)
        quote = fetch_quote(code)
        if quote is not None:
            store_quote(conn, quote)
        else:
            logger.warning("market.refresh: no quote fetched, keeping stored one", extra={"code": code})
    return written
