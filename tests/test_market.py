"""Tests for app.services.market -- the Dhaka Stock Exchange scrape
behind the dashboard's "Market" tab (Grameenphone / Robi Axiata).

Two halves, gated differently:

  * The PARSE and FETCH tests run everywhere. They never touch the
    network: `requests.get` is monkeypatched in every one, and the HTML
    they parse is REAL, saved verbatim from dsebd.org into
    tests/fixtures_market/ (page chrome removed, nothing else altered).
    Hand-written HTML would only prove the parser can read HTML someone
    wrote to match it -- the whole risk in this module is that DSE's
    actual markup is not what we think, so the fixtures have to be
    theirs.
  * The STORAGE tests are Postgres-gated via `pg_available()`, matching
    every other DB-backed test in this repo (tests/test_rollups.py,
    tests/test_storage.py, ...).

The values asserted below were verified against the live site on
2026-08-22 and are exactly what the fixtures contain, e.g. GP's
`Last Trading Price 247.50` and `Market Capitalization (mn)
337,304.945`. If DSE changes its template these tests do not go
green-but-wrong -- they fail, which is the point.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

from app.db.testing import migrated_schema, pg_available
from app.services import market

FIXTURES = Path(__file__).resolve().parent / "fixtures_market"


def _fixture(name: str) -> str:
    path = FIXTURES / name
    if not path.exists():  # pragma: no cover - only fires on a broken checkout
        raise FileNotFoundError(f"{path} missing; market fixtures are committed to git")
    return path.read_text(encoding="utf-8")


class _FakeResponse:
    def __init__(self, status_code: int, text: str = "") -> None:
        self.status_code = status_code
        self.text = text

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


@pytest.fixture(autouse=True)
def _clear_market_cache() -> Any:
    """The module-level cache is process-global -- clear it before AND
    after every test so one test's monkeypatched response cannot leak
    into the next (or out of this file).
    """
    market._clear_cache_for_tests()
    yield
    market._clear_cache_for_tests()


# ── parsing real pages: the quote ────────────────────────────────────


def test_parse_quote_gp_reads_every_field_from_the_real_page() -> None:
    quote = market.parse_quote("GP", _fixture("dsebd_quote_gp.html"))

    assert quote is not None
    assert quote.code == "GP"
    assert quote.name == "Grameenphone Ltd."
    assert quote.last == pytest.approx(247.50)
    assert quote.change_pct == pytest.approx(-0.92)
    assert quote.day_low == pytest.approx(247.20)
    assert quote.day_high == pytest.approx(249.80)
    assert quote.week52_low == pytest.approx(237.40)
    assert quote.week52_high == pytest.approx(311.90)
    # Thousands separators must survive the parse, not truncate it.
    assert quote.market_cap_mn == pytest.approx(337_304.945)
    assert quote.free_float_mn == pytest.approx(33_754.340)
    # The session date DSE printed, not today's date.
    assert quote.as_of == date(2026, 8, 20)


def test_parse_quote_robi_reads_the_second_instrument() -> None:
    quote = market.parse_quote("ROBI", _fixture("dsebd_quote_robi.html"))

    assert quote is not None
    assert quote.code == "ROBI"
    assert quote.name == "Robi Axiata PLC."
    assert quote.last == pytest.approx(31.30)
    # A POSITIVE change -- GP's is negative, so between the two fixtures
    # both signs of the nested Change* cell are exercised.
    assert quote.change_pct == pytest.approx(0.32)
    assert quote.day_low == pytest.approx(31.20)
    assert quote.day_high == pytest.approx(31.40)
    assert quote.week52_low == pytest.approx(26.70)
    assert quote.week52_high == pytest.approx(33.90)
    assert quote.market_cap_mn == pytest.approx(163_423.506)
    assert quote.as_of == date(2026, 8, 20)


def test_parse_quote_returns_none_rather_than_a_half_filled_record() -> None:
    """A page missing the Market Information table must yield None, not
    a Quote with zeros in it -- a wrong number on a risk dashboard is
    worse than a missing one.
    """
    truncated = _fixture("dsebd_quote_gp.html").split("Market Information:")[0]

    assert market.parse_quote("GP", truncated) is None


def test_parse_quote_on_garbage_returns_none() -> None:
    assert market.parse_quote("GP", "<html><body>not the page you wanted</body></html>") is None
    assert market.parse_quote("GP", "") is None


# ── parsing real pages: the day-end archive ──────────────────────────


def test_parse_daily_history_reads_the_real_archive_table() -> None:
    rows = market.parse_daily_history("GP", _fixture("dsebd_archive_gp.html"))

    # The fixture keeps the 60 most recent rows of the real 233-row
    # response for the same window.
    assert len(rows) == 60
    # Ascending by day, even though DSE serves newest-first.
    assert rows == sorted(rows)
    assert rows[0][0] == date(2026, 5, 21)
    assert rows[-1] == (date(2026, 8, 20), pytest.approx(247.5))
    # CLOSEP* (column 7), NOT LTP*/HIGH/LOW/OPENP*/YCP around it.
    by_day = dict(rows)
    assert by_day[date(2026, 8, 19)] == pytest.approx(249.8)
    assert by_day[date(2026, 8, 18)] == pytest.approx(248.5)
    assert by_day[date(2026, 8, 17)] == pytest.approx(251.4)


def test_parse_daily_history_spans_multiple_months() -> None:
    rows = market.parse_daily_history("GP", _fixture("dsebd_archive_gp.html"))

    assert {day.month for day, _close in rows} == {5, 6, 7, 8}
    # Only real trading days -- no weekend/holiday filler.
    assert len(rows) < 4 * 31


def test_parse_daily_history_ignores_rows_for_another_instrument() -> None:
    """The TRADING CODE column is checked, so a GP parse of the ROBI
    page yields nothing rather than silently charting Robi's prices
    under Grameenphone's name.
    """
    robi_page = _fixture("dsebd_archive_robi.html")

    assert market.parse_daily_history("ROBI", robi_page) != []
    assert market.parse_daily_history("GP", robi_page) == []


def test_parse_daily_history_on_garbage_returns_empty_list() -> None:
    assert market.parse_daily_history("GP", "<html><body><table><tr><td>x</td></tr></table></body></html>") == []
    assert market.parse_daily_history("GP", "") == []
    # A real page of the WRONG kind is the realistic failure, not noise.
    assert market.parse_daily_history("GP", _fixture("dsebd_quote_gp.html")) == []


# ── the fetch layer never raises ─────────────────────────────────────


def test_fetch_quote_returns_a_quote_from_a_real_response(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_get = Mock(return_value=_FakeResponse(200, _fixture("dsebd_quote_gp.html")))
    monkeypatch.setattr(market.requests, "get", fake_get)

    quote = market.fetch_quote("GP")

    assert quote is not None
    assert quote.last == pytest.approx(247.50)
    assert fake_get.call_args.args[0] == market.QUOTE_URL
    assert fake_get.call_args.kwargs["params"] == {"name": "GP"}


def test_fetch_quote_verifies_tls_against_the_vendored_bundle(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression guard on the whole TLS decision in this module.

    dsebd.org serves an incomplete certificate chain, so the naive fixes
    are `verify=False` or a global override. Neither is acceptable for a
    financial source whose numbers are shown as fact, so the module
    passes an explicit CA bundle instead. If anyone ever "fixes" a TLS
    error by turning verification off, this test is what stops it.
    """
    fake_get = Mock(return_value=_FakeResponse(200, _fixture("dsebd_quote_gp.html")))
    monkeypatch.setattr(market.requests, "get", fake_get)

    market.fetch_quote("GP")

    verify = fake_get.call_args.kwargs["verify"]
    assert verify is not False
    assert verify == market.CA_BUNDLE_PATH
    bundle = Path(market.CA_BUNDLE_PATH)
    assert bundle.exists(), "the vendored dsebd CA bundle must be committed alongside this module"
    # Exactly the missing intermediate plus its root anchor.
    assert bundle.read_text(encoding="utf-8").count("-----BEGIN CERTIFICATE-----") == 2


def test_fetch_quote_never_raises_on_network_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise ConnectionError("simulated network failure")

    monkeypatch.setattr(market.requests, "get", _boom)

    assert market.fetch_quote("GP") is None


def test_fetch_quote_never_raises_on_non_200(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(market.requests, "get", Mock(return_value=_FakeResponse(503, "gateway down")))

    assert market.fetch_quote("GP") is None


def test_fetch_quote_never_raises_on_garbage_html(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(market.requests, "get", Mock(return_value=_FakeResponse(200, "<h1>maintenance</h1>")))

    assert market.fetch_quote("GP") is None


def test_fetch_quote_caches_a_success_but_not_a_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    ok = Mock(return_value=_FakeResponse(200, _fixture("dsebd_quote_gp.html")))
    monkeypatch.setattr(market.requests, "get", ok)
    assert market.fetch_quote("GP") == market.fetch_quote("GP")
    ok.assert_called_once()  # second call served from cache

    market._clear_cache_for_tests()

    failing = Mock(return_value=_FakeResponse(500, ""))
    monkeypatch.setattr(market.requests, "get", failing)
    assert market.fetch_quote("GP") is None
    assert market.fetch_quote("GP") is None
    # A blip at DSE must not pin an empty tab in place for the whole TTL.
    assert failing.call_count == 2


def test_fetch_daily_history_returns_rows_and_sends_the_archive_params(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_get = Mock(return_value=_FakeResponse(200, _fixture("dsebd_archive_gp.html")))
    monkeypatch.setattr(market.requests, "get", fake_get)

    rows = market.fetch_daily_history("GP", date(2025, 9, 1), date(2026, 8, 20))

    assert len(rows) == 60
    assert fake_get.call_args.args[0] == market.ARCHIVE_URL
    assert fake_get.call_args.kwargs["params"] == {
        "startDate": "2025-09-01",
        "endDate": "2026-08-20",
        "inst": "GP",
        "archive": "data",
    }
    assert fake_get.call_args.kwargs["verify"] == market.CA_BUNDLE_PATH


def test_fetch_daily_history_never_raises_on_network_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise TimeoutError("simulated timeout")

    monkeypatch.setattr(market.requests, "get", _boom)

    assert market.fetch_daily_history("GP", date(2026, 1, 1), date(2026, 8, 20)) == []


def test_fetch_daily_history_never_raises_on_non_200(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(market.requests, "get", Mock(return_value=_FakeResponse(404, "")))

    assert market.fetch_daily_history("GP", date(2026, 1, 1), date(2026, 8, 20)) == []


def test_fetch_daily_history_never_raises_on_garbage_html(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(market.requests, "get", Mock(return_value=_FakeResponse(200, "<<< not html >>>")))

    assert market.fetch_daily_history("GP", date(2026, 1, 1), date(2026, 8, 20)) == []


# ── storage (Postgres-gated) ─────────────────────────────────────────

pg = pytest.mark.skipif(not pg_available(), reason="no reachable Postgres (set TEST_DATABASE_URL)")


@pg
def test_store_history_is_idempotent_and_corrects_restated_closes() -> None:
    with migrated_schema() as conn:
        rows = [(date(2026, 8, 18), 248.5), (date(2026, 8, 19), 249.8), (date(2026, 8, 20), 247.5)]

        assert market.store_history(conn, "GP", rows) == 3
        # Re-running the same window is a no-op, not a duplicate-key error.
        assert market.store_history(conn, "GP", rows) == 3
        assert conn.execute("SELECT count(*) AS n FROM market_daily_close").fetchone()["n"] == 3

        # DSE restates: last write wins on (code, day).
        market.store_history(conn, "GP", [(date(2026, 8, 20), 247.9)])
        stored = conn.execute(
            "SELECT close_price FROM market_daily_close WHERE code = 'GP' AND day = %s",
            (date(2026, 8, 20),),
        ).fetchone()
        assert float(stored["close_price"]) == pytest.approx(247.9)

        assert market.store_history(conn, "GP", []) == 0


@pg
def test_store_quote_keeps_one_row_per_code_and_latest_quote_reads_it_back() -> None:
    with migrated_schema() as conn:
        quote = market.parse_quote("GP", _fixture("dsebd_quote_gp.html"))
        assert quote is not None

        market.store_quote(conn, quote)
        market.store_quote(conn, quote)  # market_quote is a snapshot, not a history

        assert conn.execute("SELECT count(*) AS n FROM market_quote").fetchone()["n"] == 1

        read_back = market.latest_quote(conn, "GP")
        assert read_back == quote

        assert market.latest_quote(conn, "ROBI") is None


@pg
def test_latest_quote_round_trips_a_missing_free_float() -> None:
    """free_float_mn is the one nullable number -- DSE omits it for some
    instruments and the quote is still worth showing.
    """
    with migrated_schema() as conn:
        base = market.parse_quote("ROBI", _fixture("dsebd_quote_robi.html"))
        assert base is not None
        market.store_quote(conn, market.Quote(**{**base.__dict__, "free_float_mn": None}))

        read_back = market.latest_quote(conn, "ROBI")
        assert read_back is not None
        assert read_back.free_float_mn is None


@pg
def test_monthly_closes_returns_the_last_close_of_each_month() -> None:
    with migrated_schema() as conn:
        market.store_history(
            conn,
            "GP",
            [
                (date(2026, 5, 21), 237.7),
                (date(2026, 5, 28), 240.1),
                (date(2026, 5, 31), 241.0),  # last May trading day -> this is May's point
                (date(2026, 7, 2), 250.0),
                (date(2026, 7, 30), 252.5),  # last July trading day -> this is July's point
            ],
        )

        result = market.monthly_closes(conn, "GP", date(2026, 5, 1))

        assert [(m.month, m.close) for m in result] == [
            (date(2026, 5, 1), pytest.approx(241.0)),
            (date(2026, 7, 1), pytest.approx(252.5)),
        ]


@pg
def test_monthly_closes_omits_a_month_with_no_trading_days() -> None:
    """June had no rows above. It must be ABSENT, not present with 0.0
    and not carried forward from May -- zero would draw as a crash and a
    carried price would assert a value nobody quoted (the same honesty
    rule as migration 0021's rollup_monthly_coverage).
    """
    with migrated_schema() as conn:
        market.store_history(conn, "GP", [(date(2026, 5, 31), 241.0), (date(2026, 7, 30), 252.5)])

        months = [m.month for m in market.monthly_closes(conn, "GP", date(2026, 5, 1))]

        assert date(2026, 6, 1) not in months
        assert months == [date(2026, 5, 1), date(2026, 7, 1)]


@pg
def test_monthly_closes_respects_since_and_code() -> None:
    with migrated_schema() as conn:
        market.store_history(conn, "GP", [(date(2026, 4, 30), 230.0), (date(2026, 7, 30), 252.5)])
        market.store_history(conn, "ROBI", [(date(2026, 7, 30), 31.3)])

        gp = market.monthly_closes(conn, "GP", date(2026, 5, 1))
        assert [m.month for m in gp] == [date(2026, 7, 1)]  # April is before `since`

        robi = market.monthly_closes(conn, "ROBI", date(2026, 5, 1))
        assert [(m.month, m.close) for m in robi] == [(date(2026, 7, 1), pytest.approx(31.3))]

        assert market.monthly_closes(conn, "NOPE", date(2026, 5, 1)) == []


@pg
def test_refresh_stores_both_instruments(monkeypatch: pytest.MonkeyPatch) -> None:
    pages = {
        ("GP", "quote"): _fixture("dsebd_quote_gp.html"),
        ("GP", "archive"): _fixture("dsebd_archive_gp.html"),
        ("ROBI", "quote"): _fixture("dsebd_quote_robi.html"),
        ("ROBI", "archive"): _fixture("dsebd_archive_robi.html"),
    }

    def _fake_get(url: str, **kwargs: Any) -> _FakeResponse:
        params = kwargs["params"]
        kind = "archive" if url == market.ARCHIVE_URL else "quote"
        code = params["inst"] if kind == "archive" else params["name"]
        return _FakeResponse(200, pages[(code, kind)])

    monkeypatch.setattr(market.requests, "get", _fake_get)

    with migrated_schema() as conn:
        written = market.refresh(conn)

        assert written == {"GP": 60, "ROBI": 20}
        assert conn.execute("SELECT count(*) AS n FROM market_quote").fetchone()["n"] == 2
        assert conn.execute("SELECT count(*) AS n FROM market_daily_close").fetchone()["n"] == 80

        gp = market.latest_quote(conn, "GP")
        assert gp is not None and gp.name == "Grameenphone Ltd."


@pg
def test_refresh_keeps_stored_data_when_the_source_is_down(monkeypatch: pytest.MonkeyPatch) -> None:
    """The reason this module stores anything at all: a DSE outage must
    degrade to "here is the last known quote", never to a blank tab or a
    raised exception.
    """
    with migrated_schema() as conn:
        good = market.parse_quote("GP", _fixture("dsebd_quote_gp.html"))
        assert good is not None
        market.store_quote(conn, good)
        market.store_history(conn, "GP", [(date(2026, 8, 20), 247.5)])

        def _boom(*args: Any, **kwargs: Any) -> Any:
            raise ConnectionError("dsebd.org unreachable")

        monkeypatch.setattr(market.requests, "get", _boom)

        assert market.refresh(conn, codes=("GP",)) == {"GP": 0}

        assert market.latest_quote(conn, "GP") == good
        assert conn.execute("SELECT count(*) AS n FROM market_daily_close").fetchone()["n"] == 1
