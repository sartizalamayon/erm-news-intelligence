"""EDGE cases for the rebuilt Long-term dashboard — the four tab
endpoints and the watchlist add-flow in
`app/api/routers/dashboard_long.py`.

`tests/test_dashboard_long.py` covers the happy paths, the empty
schema, auth, and two regressions. This file is the complement: the
boundaries of every threshold, the arithmetic that has a divisor in it,
the inputs a headline can actually carry, and what the add-flow does
with input nobody would type on purpose.

Every assertion here was written AFTER running the endpoint and reading
what it returned. Where the observed answer is wrong, the test asserts
the RIGHT answer. Every defect these found has since been fixed, so
the record without breaking the suite; each of those carries the
file:line it comes from. There are five:

  * `test_economy_survives_an_unscored_axis_being_the_loudest`
    — dashboard_long.py:455-463. A 500. When the axis with the most
    items has no mean (nothing scored) and any other axis does, the
    verdict sentence formats `None` with `:.1f`. The whole Economy tab
    dies on a state the Telecom boards are explicitly built to render
    (Airtel arrived with sixteen items and none scored).

  * `test_stored_closes_draw_an_index_even_when_the_quote_is_missing`
    — dashboard_long.py:306-307. Twelve months of stored closes draw no
    index chart at all if the quote scrape failed, because the series is
    built inside the block the missing quote skips. The market half of
    this screen exists to keep answering when a fetch does not.

  * `test_a_window_with_no_monitored_month_counts_nothing`
    — dashboard_long.py:134. With no monitored month, `since_effective`
    falls back to the calendar window, so the boards count twelve months
    of backdated articles under a heading that reads "No monitored
    months yet".

  * `test_preview_refuses_a_group_key_that_does_not_exist`
    — dashboard_long.py:616. `create` refuses an unknown board with a
    422; `preview` accepts it and reports "No guard applies to this
    board", which is false about a board that does not exist.

  * `test_create_refuses_a_term_that_is_only_punctuation`
    — app/services/watchlist_admin.py:192. A term with no alphanumeric
    character in it saves, as a pattern that can never match a headline.

No socket is opened: `tests/conftest.py` blocks them process-wide and
every macro fetcher is monkeypatched, exactly as the sibling file does.
"""

from __future__ import annotations

import contextlib
import itertools
import time
from datetime import date, datetime, timedelta, timezone

import psycopg
import pytest
from fastapi.testclient import TestClient

from app.api.deps import AuthenticatedUser, get_current_user, require_admin
from app.db.testing import migrated_schema, pg_available
from app.main import create_app
from app.services import macro as macro_service

pytestmark = pytest.mark.skipif(not pg_available(), reason="no reachable Postgres (set TEST_DATABASE_URL)")

ADMIN = AuthenticatedUser(id=1, email="cro@grameenphone.com", role="admin", display_name="CRO", session_id=1)

_url_seq = itertools.count()

THIS_MONTH = date.today().replace(day=1)
LAST_MONTH = (THIS_MONTH - timedelta(days=1)).replace(day=1)

# What the screen prints when `_window()` found no month the collector
# ran in. Pinned as a constant because two tests below assert that the
# boards agree with it.
NO_MONITORED_LABEL = "No monitored months yet"


# ── seeding ──────────────────────────────────────────────────────────


def _insert(conn, **fields) -> int:
    defaults = {
        "url": f"https://x.test/e-{next(_url_seq)}",
        "url_norm": "",
        "source": "Prothom Alo",
        "title": "Test headline",
        "title_en": "",
        "day": THIS_MONTH,
        "sections": "",
        "keywords": "",
        "review_status": "pending",
    }
    defaults.update(fields)
    cols = list(defaults)
    row = conn.execute(
        f"INSERT INTO articles ({', '.join(cols)}) VALUES "
        f"({', '.join('%(' + c + ')s' for c in cols)}) RETURNING id",
        defaults,
    ).fetchone()
    return int(row["id"])


def _coverage(conn, month: date, days: int) -> None:
    conn.execute(
        "INSERT INTO rollup_monthly_coverage (month, article_count, matched_count, scored_count, "
        "collected_count, collected_days) VALUES (%s, 0, 0, 0, 0, %s) "
        "ON CONFLICT (month) DO UPDATE SET collected_days = EXCLUDED.collected_days",
        (month, days),
    )


def _section(conn, month: date, matched: int, section: str = "Political") -> None:
    conn.execute(
        "INSERT INTO rollup_monthly_section (month, section, fetched_count, matched_count) "
        "VALUES (%s, %s, %s, %s) ON CONFLICT (month, section) DO UPDATE SET "
        "matched_count = EXCLUDED.matched_count",
        (month, section, matched, matched),
    )


# Every NOT NULL column of market_quote, so a test can override the one
# number it is actually about.
_QUOTE = {
    "name": "Grameenphone Ltd.",
    "last_price": 250.0,
    "change_pct": 0.5,
    "day_low": 240.0,
    "day_high": 260.0,
    "week52_low": 200.0,
    "week52_high": 300.0,
    "market_cap_mn": 1000.0,
    "free_float_mn": 100.0,
    "as_of": date.today(),
}


def _seed_market(conn, code: str, closes=(), quote: dict | None = None) -> None:
    for day, close in closes:
        conn.execute(
            "INSERT INTO market_daily_close (code, day, close_price) VALUES (%s, %s, %s) "
            "ON CONFLICT (code, day) DO UPDATE SET close_price = EXCLUDED.close_price",
            (code, day, close),
        )
    if quote is not None:
        merged = dict(_QUOTE)
        merged.update(quote)
        cols = ["code", *merged.keys()]
        vals = [code, *merged.values()]
        conn.execute(
            f"INSERT INTO market_quote ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))}) "
            f"ON CONFLICT (code) DO UPDATE SET {', '.join(f'{c} = EXCLUDED.{c}' for c in cols[1:])}",
            vals,
        )


def _twelve_month_ends() -> list[date]:
    """Mid-month trading days across the twelve months the market tab
    reads, oldest first."""
    return [date(THIS_MONTH.year, THIS_MONTH.month, 15) - timedelta(days=30 * i) for i in range(11, -1, -1)]


def _row(payload: dict, board: str, key: str) -> dict:
    return next(r for r in payload[board]["rows"] if r["key"] == key)


def _line(payload: dict, key: str) -> dict:
    return next(ln for ln in payload["lines"] if ln["key"] == key)


# ── fixtures ─────────────────────────────────────────────────────────


@pytest.fixture
def offline_macro(monkeypatch: pytest.MonkeyPatch):
    """No third party is reachable from a test, and none should be."""
    now = datetime.now(timezone.utc)
    monkeypatch.setattr(macro_service, "fetch_usd_bdt", lambda: macro_service.MacroValue(122.0, now))
    monkeypatch.setattr(macro_service, "fetch_brent", lambda key: macro_service.MacroValue(93.6, now))
    monkeypatch.setattr(macro_service, "fetch_inflation", lambda: macro_service.MacroValue(8.77, now))
    monkeypatch.setattr(
        macro_service, "fetch_annual_exchange_rate", lambda: macro_service.AnnualValue(121.92, 2025, now)
    )


@contextlib.contextmanager
def _app_client(monkeypatch: pytest.MonkeyPatch, *, raise_server_exceptions: bool):
    import app.api.routers.dashboard_long as mod

    with migrated_schema() as conn:
        monkeypatch.setattr(mod, "get_connection", lambda: contextlib.nullcontext(conn))
        app = create_app()
        app.dependency_overrides[get_current_user] = lambda: ADMIN
        app.dependency_overrides[require_admin] = lambda: ADMIN
        with TestClient(app, raise_server_exceptions=raise_server_exceptions) as c:
            c.conn = conn  # type: ignore[attr-defined]
            yield c


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, offline_macro):
    with _app_client(monkeypatch, raise_server_exceptions=True) as c:
        yield c


@pytest.fixture
def soft_client(monkeypatch: pytest.MonkeyPatch, offline_macro):
    """Same client, but a handler that raises comes back as a 500 rather
    than re-raising into the test. Used only where the observed answer
    IS a 500 — the status code is a far more readable failure than a
    re-raised TypeError inside an anyio ExceptionGroup.
    """
    with _app_client(monkeypatch, raise_server_exceptions=False) as c:
        yield c


# ═════════════════════════════════════════════════════════════════════
# 1. Boundary scores — RED 8-10, YELLOW 6-7, GREEN 5, MAGENTA 1-4
# ═════════════════════════════════════════════════════════════════════

# One operator per band boundary. `initiatives` returns the top item per
# operator with a floor of 4.0, so it is the only place on the wire that
# carries a band below red — the crit lists and `moved` are 8.0-and-up
# by construction.
_BAND_CASES = [
    ("Grameenphone", 10.0, "red"),
    ("Robi Axiata", 8.0, "red"),
    ("Banglalink", 7.999, "yellow"),
    ("Teletalk", 6.0, "yellow"),
    ("Starlink", 5.0, "green"),
    ("Airtel", 4.999, "magenta"),
]


def test_every_rubric_boundary_lands_in_the_right_band(client):
    """8.0 is red and 7.999 is not; 5.0 is green and 4.999 is not.

    `llm_score` is a REAL (migrations/0003), so a thousandth below a
    boundary really does store below it rather than rounding onto it.
    """
    conn = client.conn
    _coverage(conn, THIS_MONTH, days=5)
    for operator, score, _band in _BAND_CASES:
        _insert(conn, title=f"{operator} boundary item", llm_score=score, llm_reason="r")
    conn.commit()

    bands = {i["title"]: (i["score"], i["band"]) for i in client.get("/dashboard/long/telecom").json()["initiatives"]}
    assert bands == {
        f"{operator} boundary item": (pytest.approx(score, abs=1e-3), band)
        for operator, score, band in _BAND_CASES
    }


def test_the_critical_count_is_inclusive_at_eight_and_nowhere_below(client):
    """`critical` is `llm_score >= 8.0`. Seven scores spanning every
    band must yield exactly the two at or above it — and the row's other
    numbers must not shift with them.
    """
    conn = client.conn
    _coverage(conn, THIS_MONTH, days=5)
    for score in (10.0, 8.0, 7.999, 6.0, 5.0, 4.999, 1.0):
        _insert(conn, title=f"BTRC boundary {score}", llm_score=score, llm_reason="r")
    conn.commit()

    btrc = _row(client.get("/dashboard/long/telecom").json(), "regulatory", "btrc")
    assert (btrc["items"], btrc["scored"], btrc["critical"]) == (7, 7, 2)
    # (10 + 8 + 7.999 + 6 + 5 + 4.999 + 1) / 7 = 6.1426…, one decimal on the wire.
    assert btrc["mean"] == 6.1


def test_the_moved_list_uses_the_same_eight_point_boundary(client):
    """"What moved" on the Risk tab is the same 8.0 floor, so a 7.999
    item must not appear beside an 8.0 one."""
    conn = client.conn
    _coverage(conn, THIS_MONTH, days=5)
    _insert(conn, title="Exactly eight", llm_score=8.0, llm_reason="r")
    _insert(conn, title="A thousandth under eight", llm_score=7.999, llm_reason="r")
    conn.commit()

    moved = client.get("/dashboard/long/risk").json()["moved"]
    assert [m["title"] for m in moved] == ["Exactly eight"]
    assert moved[0]["band"] == "red"


def test_the_initiative_floor_admits_exactly_four_and_refuses_just_below(client):
    """`initiatives` floors at 4.0 — the bottom of MAGENTA as the board
    uses it. The sibling suite pins that a 1.0 item is out; this pins
    where the line actually falls."""
    conn = client.conn
    _coverage(conn, THIS_MONTH, days=5)
    _insert(conn, title="Teletalk at exactly four", llm_score=4.0, llm_reason="r")
    _insert(conn, title="Starlink a thousandth under four", llm_score=3.999, llm_reason="r")
    conn.commit()

    titles = {i["title"] for i in client.get("/dashboard/long/telecom").json()["initiatives"]}
    assert titles == {"Teletalk at exactly four"}


# ═════════════════════════════════════════════════════════════════════
# 2. Window boundaries
# ═════════════════════════════════════════════════════════════════════


def test_the_first_day_of_the_first_monitored_month_is_in_and_the_day_before_is_out(client):
    """The window is `day >= first monitored month`, inclusive at the
    bottom. One day either side of that edge decides whether a headline
    is part of the sample or an accident of a later crawl."""
    conn = client.conn
    _coverage(conn, LAST_MONTH, days=9)
    _coverage(conn, THIS_MONTH, days=5)
    _insert(conn, title="BTRC on the first monitored day", llm_score=9.0, llm_reason="r", day=LAST_MONTH)
    _insert(
        conn,
        title="BTRC the day before the window",
        llm_score=9.0,
        llm_reason="r",
        day=LAST_MONTH - timedelta(days=1),
    )
    conn.commit()

    btrc = _row(client.get("/dashboard/long/telecom").json(), "regulatory", "btrc")
    assert btrc["items"] == 1
    assert [c["title"] for c in btrc["crit"]] == ["BTRC on the first monitored day"]


def test_a_month_with_zero_collected_days_never_starts_the_window(client):
    """A coverage row with `collected_days = 0` is a month the collector
    did not run in. Articles dated inside it are backdated finds, and
    must not be counted or used as the window's start."""
    conn = client.conn
    _coverage(conn, LAST_MONTH, days=0)
    _coverage(conn, THIS_MONTH, days=5)
    _insert(conn, title="BTRC in the unmonitored month", llm_score=9.0, llm_reason="r", day=LAST_MONTH)
    _insert(conn, title="BTRC in the monitored month", llm_score=9.0, llm_reason="r", day=THIS_MONTH)
    conn.commit()

    payload = client.get("/dashboard/long/telecom").json()
    assert payload["regulatory"]["window"] == f"{THIS_MONTH.strftime('%b %Y')} · 5 collection days"
    btrc = _row(payload, "regulatory", "btrc")
    assert btrc["items"] == 1
    assert [c["title"] for c in btrc["crit"]] == ["BTRC in the monitored month"]


def test_a_month_with_articles_and_no_coverage_row_is_not_a_monitored_month(client):
    """No coverage row at all is the same claim as zero days: nobody
    recorded the collector running. The label has to say so."""
    conn = client.conn
    _insert(conn, title="BTRC with no coverage row behind it", llm_score=9.0, llm_reason="r")
    conn.commit()

    payload = client.get("/dashboard/long/telecom").json()
    assert payload["regulatory"]["window"] == NO_MONITORED_LABEL
    assert payload["peers"]["window"] == NO_MONITORED_LABEL


def test_a_window_with_no_monitored_month_counts_nothing(client):
    conn = client.conn
    _coverage(conn, THIS_MONTH, days=0)
    _insert(conn, title="BTRC in a month nobody collected", llm_score=9.0, llm_reason="r")
    _insert(conn, title="Grameenphone in a month nobody collected", llm_score=9.0, llm_reason="r")
    conn.commit()

    payload = client.get("/dashboard/long/telecom").json()
    assert payload["regulatory"]["window"] == NO_MONITORED_LABEL
    counted = [
        (board, r["key"], r["items"])
        for board in ("peers", "wallets", "regulatory")
        for r in payload[board]["rows"]
        if r["items"]
    ]
    assert counted == [], "a board cannot report items under a window that says no month was monitored"


def test_a_single_monitored_month_degrades_instead_of_dividing_by_zero(client):
    """One monitored month means there is no previous month to compare
    against. The political line must print the level and say it is a
    level, not attempt a month-over-month rate."""
    conn = client.conn
    _coverage(conn, THIS_MONTH, days=5)
    _section(conn, THIS_MONTH, 10)
    conn.commit()

    political = _line(client.get("/dashboard/long/risk").json(), "political")
    assert political["value"] == "10"
    assert political["direction"] == "flat"
    assert political["directionLabel"] == "Steady"
    assert political["sub"] == f"matched items in {THIS_MONTH.strftime('%b %Y')}"
    assert political["spark"] == []


def test_a_previous_month_of_zero_political_items_does_not_divide_by_zero(client):
    """Two monitored months, the earlier one empty. The percentage change
    has a zero denominator, so the line falls back to the level rather
    than reporting an infinite rise."""
    conn = client.conn
    _coverage(conn, LAST_MONTH, days=9)
    _coverage(conn, THIS_MONTH, days=5)
    _section(conn, LAST_MONTH, 0)
    _section(conn, THIS_MONTH, 10)
    conn.commit()

    political = _line(client.get("/dashboard/long/risk").json(), "political")
    assert political["value"] == "10"
    assert political["directionLabel"] == "Steady"
    assert political["sub"] == f"matched items in {THIS_MONTH.strftime('%b %Y')}"


# ═════════════════════════════════════════════════════════════════════
# 3. Market maths — every divisor on the tab
# ═════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("first_close", [0.0, -5.0])
def test_a_first_close_of_zero_or_less_is_refused_as_a_rebase_base(client, first_close):
    """The index rebases every close against the first month's. A base
    of zero or below cannot produce a percentage, and the tab must drop
    the series rather than publish one."""
    conn = client.conn
    months = _twelve_month_ends()
    _seed_market(conn, "GP", [(m, first_close if i == 0 else 100.0) for i, m in enumerate(months)], quote={})
    _seed_market(conn, "ROBI", [(m, 50.0) for m in months], quote={"name": "Robi Axiata Ltd."})
    conn.commit()

    payload = client.get("/dashboard/long/market").json()
    assert [s["code"] for s in payload["series"]] == ["ROBI"], "GP cannot be rebased against a non-positive base"

    competitive = _line(client.get("/dashboard/long/risk").json(), "competitive")
    assert competitive["value"] == "—"
    assert competitive["direction"] == "none"
    assert competitive["sub"] == "Stored closes are unusable."


def test_a_zero_close_never_becomes_a_month_over_month_percentage(client):
    """`moms` divides by the previous month's close. A zero there is
    reported as no reading, not as an infinite move."""
    conn = client.conn
    months = _twelve_month_ends()
    _seed_market(conn, "GP", [(m, 0.0 if i == 0 else 100.0) for i, m in enumerate(months)], quote={})
    conn.commit()

    rows = client.get("/dashboard/long/market").json()["months"]
    assert rows[0]["closes"]["GP"] == 0.0
    assert rows[0]["moms"]["GP"] is None
    assert rows[1]["moms"]["GP"] is None, "a move measured against a zero close is not a percentage"


def test_a_single_stored_month_cannot_be_an_index_or_a_gap(client):
    """One month is a point, not a line. The chart stays empty and the
    competitive line says it has nothing to compare."""
    conn = client.conn
    _seed_market(conn, "GP", [(THIS_MONTH.replace(day=15), 100.0)], quote={})
    conn.commit()

    payload = client.get("/dashboard/long/market").json()
    assert payload["series"] == []
    assert len(payload["months"]) == 1
    assert payload["labels"] == [THIS_MONTH.strftime("%b")]
    assert payload["months"][0]["moms"]["GP"] is None
    assert payload["tradingDays"] == 1

    competitive = _line(client.get("/dashboard/long/risk").json(), "competitive")
    assert competitive["value"] == "—"
    assert competitive["sub"] == "Not enough stored exchange history to compare yet."


def test_a_degenerate_52_week_range_does_not_divide_by_zero(client):
    """A 52-week high equal to its low makes `rangePos`'s divisor zero.

    `rangePos` now comes back None rather than 0.0. That distinction is
    the point: 0.0 rendered as "sitting at its 52-week low", a claim
    about an instrument whose position in the range is in fact
    undefined. None lets the panel draw a hatched rule instead, like
    every other absent measurement on this screen.

    `belowHighPct` deliberately stays 0.0 here and is NOT None: with the
    last price equal to the high, "0% below the high" is a true
    statement, not a missing measurement. Only the position inside a
    zero-width range is undefined.
    """
    conn = client.conn
    _seed_market(conn, "GP", [], quote={"week52_low": 250.0, "week52_high": 250.0, "last_price": 250.0})
    conn.commit()

    r = client.get("/dashboard/long/market")
    assert r.status_code == 200
    inst = r.json()["instruments"][0]
    assert inst["w52Low"] == inst["w52High"] == 250.0
    assert inst["rangePos"] is None
    assert inst["belowHighPct"] == 0.0


def test_a_quote_with_no_stored_closes_draws_the_panel_and_no_chart(client):
    """The header numbers come from the quote and the chart from the
    archive. Having one and not the other must not fabricate the other."""
    conn = client.conn
    _seed_market(conn, "GP", [], quote={})
    conn.commit()

    payload = client.get("/dashboard/long/market").json()
    assert [i["code"] for i in payload["instruments"]] == ["GP"]
    assert payload["series"] == []
    assert payload["labels"] == []
    assert payload["months"] == []
    assert payload["window"] == ""
    assert payload["tradingDays"] == 0


def test_closes_with_no_quote_still_fill_the_month_table(client):
    """A failed quote scrape must not erase the stored archive: the
    month-by-month table and the trading-day count are read straight
    from `market_daily_close`."""
    conn = client.conn
    months = _twelve_month_ends()
    _seed_market(conn, "GP", [(m, 100.0 - i) for i, m in enumerate(months)])
    conn.commit()

    payload = client.get("/dashboard/long/market").json()
    assert payload["instruments"] == []
    assert payload["asOf"] == ""
    assert len(payload["months"]) == 12
    assert payload["tradingDays"] == 12
    assert payload["months"][-1]["closes"]["GP"] == 89.0


def test_stored_closes_draw_an_index_even_when_the_quote_is_missing(client):
    conn = client.conn
    months = _twelve_month_ends()
    _seed_market(conn, "GP", [(m, 100.0 - i) for i, m in enumerate(months)])
    conn.commit()

    payload = client.get("/dashboard/long/market").json()
    assert [s["code"] for s in payload["series"]] == ["GP"]
    assert payload["series"][0]["values"][0] == 100.0


# ═════════════════════════════════════════════════════════════════════
# 4. Empty and hostile strings
# ═════════════════════════════════════════════════════════════════════

# "What moved" reads the predicate TRUE with a floor of 8.0, so a single
# scored article round-trips through it whatever its text says. That is
# the narrowest way to check a title survives the wire unmangled.
_HOSTILE_TITLES = {
    "empty": "",
    "five_thousand_chars": "BTRC " + "x" * 4995,
    "script_tag": "<script>alert('BTRC')</script>",
    "printf": "%s %d %(name)s BTRC",
    "format_braces": "{{}} {0} {label} BTRC",
    "escaped_nul": "BTRC \\x00 \\u0000 \x01\x1f end",
    "bangla": "বিটিআরসি অভিযান চালিয়েছে",
    "quotes_and_backslash": "BTRC's \"quoted\" — \\ backslash",
}


@pytest.mark.parametrize("case", sorted(_HOSTILE_TITLES), ids=sorted(_HOSTILE_TITLES))
def test_a_hostile_title_answers_200_and_round_trips_unmangled(client, case):
    title = _HOSTILE_TITLES[case]
    conn = client.conn
    _coverage(conn, THIS_MONTH, days=5)
    _insert(conn, title=title, llm_score=9.0, llm_reason="r")
    conn.commit()

    r = client.get("/dashboard/long/risk")
    assert r.status_code == 200, r.text
    moved = r.json()["moved"]
    assert len(moved) == 1
    assert moved[0]["title"] == title
    assert len(moved[0]["title"]) == len(title)


def test_an_empty_title_falls_back_to_the_translation_rather_than_to_nothing(client):
    """`coalesce(nullif(title_en, ''), title)` — an empty source
    headline is shown as its English translation when one exists, which
    is the only text there is."""
    conn = client.conn
    _coverage(conn, THIS_MONTH, days=5)
    _insert(conn, title="", title_en="BTRC fines an operator", llm_score=9.0, llm_reason="r")
    conn.commit()

    btrc = _row(client.get("/dashboard/long/telecom").json(), "regulatory", "btrc")
    assert btrc["items"] == 1
    assert [c["title"] for c in btrc["crit"]] == ["BTRC fines an operator"]


def test_every_tab_answers_200_with_a_corpus_of_nothing_but_hostile_titles(client):
    conn = client.conn
    _coverage(conn, THIS_MONTH, days=5)
    for name, title in _HOSTILE_TITLES.items():
        fallback = "" if title else "BTRC empty-title fallback"
        _insert(conn, title=title, title_en=fallback, llm_score=9.0, llm_reason=name)
    conn.commit()

    for tab in ("risk", "market", "economy", "telecom"):
        assert client.get(f"/dashboard/long/{tab}").status_code == 200, tab
    preview = client.post("/watchlist/preview", json={"enTerms": ["btrc"], "groupKey": "regulatory"})
    assert preview.status_code == 200, preview.text
    # Seven of the eight carry the Latin token "btrc"; the Bangla one
    # spells it বিটিআরসি and a Latin term correctly does not reach it.
    assert preview.json()["unguardedTotal"] == 7


def test_a_real_nul_byte_can_never_reach_the_dashboard(client):
    """The one hostile string the endpoints never have to survive: the
    driver refuses a NUL at insert, so no article can carry one. Pinned
    so a future change to the write path does not quietly hand this
    screen a value Postgres would not store.
    """
    conn = client.conn
    _coverage(conn, THIS_MONTH, days=5)
    _insert(conn, title="BTRC ordinary headline", llm_score=9.0, llm_reason="r")
    conn.commit()

    with pytest.raises(psycopg.DataError, match="NUL"):
        _insert(conn, title="BTRC \x00 embedded nul", llm_score=9.0, llm_reason="r")
    conn.rollback()

    r = client.get("/dashboard/long/risk")
    assert r.status_code == 200, r.text
    assert [m["title"] for m in r.json()["moved"]] == ["BTRC ordinary headline"]


# ═════════════════════════════════════════════════════════════════════
# 5. Watchlist add-flow
# ═════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    ("body", "detail"),
    [
        pytest.param(
            {"enTerms": [], "bnTerms": [], "groupKey": "mfs"},
            "at least one English or Bangla term is required",
            id="no_terms",
        ),
        pytest.param(
            {"enTerms": [f"term{i}" for i in range(50)], "groupKey": "mfs"},
            "at most 12 English terms, got 50",
            id="fifty_terms",
        ),
        pytest.param({"enTerms": ["a" * 500], "groupKey": "mfs"}, "longer than 60 characters", id="500_char_term"),
        pytest.param({"enTerms": ["  "], "groupKey": "mfs"}, "at least one English", id="whitespace_only"),
        pytest.param({"enTerms": ["x"], "groupKey": "mfs"}, "too short", id="single_character"),
        pytest.param(
            {"enTerms": ["নগদ"], "groupKey": "mfs"},
            "put it in the Bangla field",
            id="bangla_in_the_english_box",
        ),
        pytest.param(
            {"bnTerms": ["nagad"], "groupKey": "mfs"},
            "put Latin-script terms in the English field",
            id="latin_in_the_bangla_box",
        ),
        pytest.param(
            {"enTerms": ["upay"], "context": "no-such-guard"},
            "unknown guard 'no-such-guard'",
            id="unknown_guard",
        ),
    ],
)
def test_preview_refuses_bad_input_with_a_message_that_names_the_problem(client, body, detail):
    r = client.post("/watchlist/preview", json=body)
    assert r.status_code == 422, r.text
    assert detail in r.json()["detail"]


def test_preview_refuses_a_group_key_that_does_not_exist(client):
    r = client.post("/watchlist/preview", json={"enTerms": ["upay"], "groupKey": "no-such-board"})
    assert r.status_code == 422, r.text


def test_preview_survives_a_term_of_pure_punctuation_without_matching_everything(client):
    """A term with no letter or digit in it is refused outright.

    Escaping already made `...!!!` six literal characters rather than a
    wildcard, so it could never match everything. The remaining problem
    was the mirror case: it could never match ANYTHING either, and an
    entity that always reports zero is indistinguishable on the board
    from one nobody is writing about. Preview refuses it for the same
    reason create does.
    """
    conn = client.conn
    _insert(conn, title="Completely unrelated headline")
    conn.commit()

    r = client.post("/watchlist/preview", json={"enTerms": ["...!!!"], "groupKey": "mfs"})
    assert r.status_code == 422, r.text
    assert "letters or digits" in r.json()["detail"]


@pytest.mark.parametrize(
    ("body", "detail"),
    [
        pytest.param({"groupKey": "mfs", "label": "", "enTerms": ["upay"]}, "a label is required", id="empty_label"),
        pytest.param(
            {"groupKey": "mfs", "label": "   ", "enTerms": ["upay"]}, "a label is required", id="whitespace_label"
        ),
        pytest.param(
            {"groupKey": "mfs", "label": "x" * 200, "enTerms": ["upay"]},
            "label is longer than 120 characters",
            id="long_label",
        ),
        pytest.param(
            {"groupKey": "mfs", "label": "Upay", "enTerms": []},
            "at least one English or Bangla term is required",
            id="no_terms",
        ),
        pytest.param(
            {"groupKey": "mfs", "label": "Upay", "enTerms": ["a" * 500]},
            "longer than 60 characters",
            id="500_char_term",
        ),
        pytest.param(
            {"groupKey": "mfs", "label": "Upay", "enTerms": [f"term{i}" for i in range(50)]},
            "at most 12 English terms, got 50",
            id="fifty_terms",
        ),
        pytest.param(
            {"groupKey": "no-such-board", "label": "Upay", "enTerms": ["upay"]},
            "unknown watch group 'no-such-board'",
            id="unknown_group",
        ),
        pytest.param(
            {"groupKey": "mfs", "label": "Upay", "enTerms": ["upay"], "context": "no-such-guard"},
            "unknown guard 'no-such-guard'",
            id="unknown_guard",
        ),
    ],
)
def test_create_refuses_bad_input_with_a_message_that_names_the_problem(client, body, detail):
    r = client.post("/watchlist", json=body)
    assert r.status_code == 422, r.text
    assert detail in r.json()["detail"]


def test_create_refuses_a_term_that_is_only_punctuation(client):
    r = client.post("/watchlist", json={"groupKey": "mfs", "label": "Punct", "enTerms": ["...!!!"]})
    assert r.status_code == 422, r.text


def test_a_label_written_only_in_bangla_gets_a_derived_key_and_still_appears(client):
    """A label with no Latin in it slugs to nothing, so the service
    falls back to a hash of the label. The entity has to survive that
    and land on the board it was filed under."""
    r = client.post("/watchlist", json={"groupKey": "mfs", "label": "ওয়ালেট", "bnTerms": ["ওয়ালেট"]})
    assert r.status_code == 201, r.text
    created = r.json()
    assert created["label"] == "ওয়ালেট"
    assert created["key"].startswith("custom_")

    rows = client.get("/dashboard/long/telecom").json()["wallets"]["rows"]
    assert [row["name"] for row in rows if row["key"] == created["key"]] == ["ওয়ালেট"]


def test_the_same_bangla_label_twice_collides_with_itself(client):
    """The hashed key is deterministic, so a duplicate Bangla label
    fails on the same UNIQUE constraint a Latin one would."""
    first = client.post("/watchlist", json={"groupKey": "mfs", "label": "ওয়ালেট", "bnTerms": ["ওয়ালেট"]})
    assert first.status_code == 201, first.text
    second = client.post("/watchlist", json={"groupKey": "mfs", "label": "ওয়ালেট", "bnTerms": ["ওয়ালেট"]})
    assert second.status_code == 422, second.text
    assert "already exists" in second.json()["detail"]


def test_two_entities_cannot_share_a_label_even_across_boards(client):
    """`watch_entities.key` is UNIQUE table-wide, not per group, and the
    key is derived from the label — so "Upay" on the wallet board blocks
    "upay" on the operator board, and the caller is told why."""
    first = client.post("/watchlist", json={"groupKey": "mfs", "label": "Upay", "enTerms": ["upay"]})
    assert first.status_code == 201, first.text

    same_board = client.post("/watchlist", json={"groupKey": "mfs", "label": "Upay", "enTerms": ["upay"]})
    other_board = client.post("/watchlist", json={"groupKey": "operators", "label": "upay", "enTerms": ["upay"]})
    assert (same_board.status_code, other_board.status_code) == (422, 422)
    assert "already exists" in same_board.json()["detail"]
    assert "already exists" in other_board.json()["detail"]

    entities = client.get("/dashboard/long/telecom").json()["watchlist"]["entities"]
    assert len([e for e in entities if e["label"].lower() == "upay"]) == 1


def test_deleting_the_same_entity_twice_is_204_then_404(client):
    created = client.post("/watchlist", json={"groupKey": "mfs", "label": "Upay", "enTerms": ["upay"]})
    assert created.status_code == 201, created.text
    entity_id = created.json()["id"]

    assert client.delete(f"/watchlist/{entity_id}").status_code == 204
    assert client.delete(f"/watchlist/{entity_id}").status_code == 404
    assert client.get("/dashboard/long/telecom").status_code == 200


@pytest.mark.parametrize("entity_id", ["0", "-1", "abc", "1.5"])
def test_deleting_an_id_that_is_not_a_positive_integer_is_refused_at_the_boundary(client, entity_id):
    assert client.delete(f"/watchlist/{entity_id}").status_code == 422


# ═════════════════════════════════════════════════════════════════════
# 6. The Economy verdict — a 500 on a state the other boards render
# ═════════════════════════════════════════════════════════════════════


def test_economy_survives_an_unscored_axis_being_the_loudest(soft_client):
    conn = soft_client.conn
    _coverage(conn, THIS_MONTH, days=5)
    for i in range(10):
        _insert(conn, title=f"Load shedding hits Dhaka {i}", llm_score=None)
    _insert(conn, title="Bangladesh Bank tightens NPL rules", llm_score=9.0, llm_reason="r")
    conn.commit()

    assert soft_client.get("/dashboard/long/economy").status_code == 200


def test_economy_writes_a_verdict_when_both_axes_it_names_are_scored(soft_client):
    """The same shape with the loud axis scored — the path that works,
    pinned beside the one that does not so the difference is visible."""
    conn = soft_client.conn
    _coverage(conn, THIS_MONTH, days=5)
    for i in range(10):
        _insert(conn, title=f"Load shedding hits Dhaka {i}", llm_score=3.0, llm_reason="r")
    _insert(conn, title="Bangladesh Bank tightens NPL rules", llm_score=9.0, llm_reason="r")
    conn.commit()

    r = soft_client.get("/dashboard/long/economy")
    assert r.status_code == 200, r.text
    verdict = r.json()["board"]["verdict"]
    assert "Energy and power" in verdict
    assert "Banking sector" in verdict


# ═════════════════════════════════════════════════════════════════════
# 7. Scale
# ═════════════════════════════════════════════════════════════════════

# Generous enough that a loaded developer machine does not turn a
# performance observation into a red suite. The measured numbers are
# asserted against ~2s only as a reported finding, not as a gate — see
# the assertion message, which carries every timing.
_SCALE_CEILING_SECONDS = 5.0

_SCALE_SUBJECTS = [
    "Grameenphone",
    "Robi Axiata",
    "Banglalink",
    "Teletalk",
    "Starlink",
    "Airtel",
    "BTRC",
    "bKash mobile banking",
    "Nagad wallet payment",
    "Load shedding",
    "Bangladesh Bank",
    "NBR revenue board",
    "spectrum auction",
    "submarine cable",
]


def test_every_tab_still_answers_with_two_thousand_articles_in_the_window(client):
    conn = client.conn
    _coverage(conn, LAST_MONTH, days=18)
    _coverage(conn, THIS_MONTH, days=20)
    _section(conn, LAST_MONTH, 80)
    _section(conn, THIS_MONTH, 100)
    for i in range(2000):
        _insert(
            conn,
            title=f"{_SCALE_SUBJECTS[i % len(_SCALE_SUBJECTS)]} story {i}",
            # A third of the corpus unscored, the rest spread across
            # every band, which is roughly the real shape.
            llm_score=(i % 11) * 1.0 if i % 3 else None,
            llm_reason="reason" if i % 3 else "",
            day=(THIS_MONTH if i % 2 else LAST_MONTH) + timedelta(days=i % 10),
        )
    months = _twelve_month_ends()
    _seed_market(conn, "GP", [(m, 100.0 - i) for i, m in enumerate(months)], quote={})
    _seed_market(conn, "ROBI", [(m, 50.0) for m in months], quote={"name": "Robi Axiata Ltd."})
    conn.commit()

    timings: dict[str, float] = {}
    for tab in ("risk", "market", "economy", "telecom"):
        start = time.perf_counter()
        r = client.get(f"/dashboard/long/{tab}")
        timings[tab] = time.perf_counter() - start
        assert r.status_code == 200, f"{tab}: {r.text[:400]}"
    start = time.perf_counter()
    preview = client.post("/watchlist/preview", json={"enTerms": ["spectrum"], "groupKey": "regulatory"})
    timings["watchlist/preview"] = time.perf_counter() - start
    assert preview.status_code == 200, preview.text

    report = ", ".join(f"{name} {seconds:.3f}s" for name, seconds in timings.items())
    slow = {name: seconds for name, seconds in timings.items() if seconds > _SCALE_CEILING_SECONDS}
    assert not slow, f"over {_SCALE_CEILING_SECONDS}s on 2,000 articles — {report}"


def test_the_boards_still_carry_their_evidence_at_two_thousand_articles(client):
    """Volume must not silently drop the drill-down: each row still caps
    its critical items at three and says so."""
    conn = client.conn
    _coverage(conn, THIS_MONTH, days=20)
    for i in range(2000):
        _insert(
            conn,
            title=f"{_SCALE_SUBJECTS[i % len(_SCALE_SUBJECTS)]} story {i}",
            llm_score=9.0,
            llm_reason="reason",
            day=THIS_MONTH + timedelta(days=i % 10),
        )
    conn.commit()

    btrc = _row(client.get("/dashboard/long/telecom").json(), "regulatory", "btrc")
    assert btrc["items"] >= 140
    assert btrc["critical"] == btrc["items"]
    assert len(btrc["crit"]) == 3
    assert btrc["criticalTruncated"] is True
    assert btrc["critExpired"] is False
