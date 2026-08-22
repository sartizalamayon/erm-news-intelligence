"""Tests for the rebuilt Long-term dashboard: the four tab endpoints and
the watchlist add-flow (app/api/routers/dashboard_long.py), plus the risk
lines they compose (app/services/risklines.py).

Two of these are REGRESSION tests for defects found while wiring the
screen up, and both are the kind that returns plausible numbers rather
than raising, which is why they are pinned here:

  * `test_initiatives_never_leak_past_the_window_or_score_floor` — the
    per-operator clauses were joined with OR and handed to a caller that
    prefixed "day >= %s AND llm_score >= %s AND ". AND binds tighter
    than OR, so the window and the score floor applied to the FIRST
    operator only and every other operator's unscored articles came
    back. It surfaced as a TypeError on a NULL score; had every article
    been scored it would have silently widened the board instead.

  * `test_competitive_line_reads_the_market_window_not_the_news_window` —
    the competitive line was handed the news window, so with two months
    of collection it compared two closes instead of twelve and reported
    a −2.5pt gap where the true twelve-month gap was −22.9. Nothing
    failed; the number was just wrong, and wrong in the direction that
    makes a risk look smaller than it is.

Every test runs against a real migrated Postgres schema. The macro and
exchange fetchers are monkeypatched throughout — tests/conftest.py
blocks sockets process-wide, and a dashboard that silently depended on
a live third party in CI would be exactly the fragility this screen is
meant to expose.
"""

from __future__ import annotations

import contextlib
import itertools
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.api.deps import AuthenticatedUser, get_current_user, require_admin
from app.db.testing import migrated_schema, pg_available
from app.main import create_app
from app.services import macro as macro_service

pytestmark = pytest.mark.skipif(not pg_available(), reason="no reachable Postgres (set TEST_DATABASE_URL)")

ADMIN = AuthenticatedUser(
    id=1, email="cro@grameenphone.com", role="admin", display_name="CRO", session_id=1
)

_url_seq = itertools.count()

# A month the collector genuinely ran in, and the month before it, both
# inside the twelve-month window every endpoint computes from today.
THIS_MONTH = date.today().replace(day=1)
LAST_MONTH = (THIS_MONTH - timedelta(days=1)).replace(day=1)


def _insert_article(conn, **fields):
    defaults = {
        "url": f"https://x.test/a-{next(_url_seq)}",
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
        f"INSERT INTO articles ({', '.join(cols)}) VALUES ({', '.join('%(' + c + ')s' for c in cols)}) RETURNING id",
        defaults,
    ).fetchone()
    return row["id"]


def _mark_monitored(conn, month: date, days: int, *, articles: int = 0, matched: int = 0, scored: int = 0):
    conn.execute(
        "INSERT INTO rollup_monthly_coverage (month, article_count, matched_count, scored_count, "
        "collected_count, collected_days) VALUES (%s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (month) DO UPDATE SET collected_days = EXCLUDED.collected_days",
        (month, articles, matched, scored, articles, days),
    )


def _seed_market(conn, code: str, closes: list[tuple[date, float]], quote: dict | None = None):
    for day, close in closes:
        conn.execute(
            "INSERT INTO market_daily_close (code, day, close_price) VALUES (%s, %s, %s) "
            "ON CONFLICT (code, day) DO UPDATE SET close_price = EXCLUDED.close_price",
            (code, day, close),
        )
    if quote:
        cols = ["code", *quote.keys()]
        vals = [code, *quote.values()]
        conn.execute(
            f"INSERT INTO market_quote ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))}) "
            f"ON CONFLICT (code) DO UPDATE SET {', '.join(f'{c} = EXCLUDED.{c}' for c in cols[1:])}",
            vals,
        )


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


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, offline_macro):
    import app.api.routers.dashboard_long as mod

    with migrated_schema() as conn:
        monkeypatch.setattr(mod, "get_connection", lambda: contextlib.nullcontext(conn))
        app = create_app()
        app.dependency_overrides[get_current_user] = lambda: ADMIN
        app.dependency_overrides[require_admin] = lambda: ADMIN
        with TestClient(app) as c:
            c.conn = conn  # type: ignore[attr-defined]
            yield c


# ── empty schema: every tab must answer honestly, not crash ──────────


@pytest.mark.parametrize("tab", ["risk", "market", "economy", "telecom"])
def test_every_tab_answers_on_an_empty_schema(client, tab):
    """A fresh deployment has no articles, no rollups and no prices.

    Every tab must still render — the screen's whole argument is that an
    absence is shown as an absence, so a 500 here would be the one
    failure mode it cannot have.
    """
    r = client.get(f"/dashboard/long/{tab}")
    assert r.status_code == 200, r.text


def test_empty_market_tab_says_so_rather_than_inventing_a_chart(client):
    d = client.get("/dashboard/long/market").json()
    assert d["instruments"] == []
    assert d["series"] == []
    assert d["tradingDays"] == 0


def test_risk_lines_with_no_data_read_as_absent_not_as_zero(client):
    lines = {ln["key"]: ln for ln in client.get("/dashboard/long/risk").json()["lines"]}
    # Competitive has no stored prices at all, so it must not claim 0.0.
    assert lines["competitive"]["value"] == "—"
    assert lines["competitive"]["direction"] == "none"
    # FX is live (monkeypatched) and must still say it has no series.
    assert lines["fx"]["value"] == "122.00"
    assert lines["fx"]["spark"] == []


# ── boards ───────────────────────────────────────────────────────────


def test_board_counts_and_mean_come_from_real_rows(client):
    conn = client.conn
    _mark_monitored(conn, THIS_MONTH, days=5)
    for score in (9.0, 8.0, 4.0):
        _insert_article(conn, title="BTRC fines an operator over SIM rules", llm_score=score, llm_reason="r")
    _insert_article(conn, title="BTRC circular on spectrum", llm_score=None)
    conn.commit()

    board = client.get("/dashboard/long/telecom").json()["regulatory"]
    btrc = next(r for r in board["rows"] if r["key"] == "btrc")
    assert btrc["items"] == 4
    assert btrc["scored"] == 3
    assert btrc["critical"] == 2  # 9.0 and 8.0
    assert btrc["mean"] == 7.0  # (9+8+4)/3, rounded at the boundary


def test_mean_is_rounded_to_one_decimal_on_the_wire(client):
    conn = client.conn
    _mark_monitored(conn, THIS_MONTH, days=5)
    for score in (9.0, 8.0, 6.0):  # mean 7.666...
        _insert_article(conn, title="BTRC action on spectrum", llm_score=score, llm_reason="r")
    conn.commit()
    board = client.get("/dashboard/long/telecom").json()["regulatory"]
    btrc = next(r for r in board["rows"] if r["key"] == "btrc")
    assert btrc["mean"] == 7.7


def test_unscored_row_reports_no_mean_rather_than_zero(client):
    """Airtel arrived with sixteen matched items and none scored.

    A mean of 0.0 would read as "measured, and calm"; None is what lets
    the screen draw a hatched rule instead of an empty bar.
    """
    conn = client.conn
    _mark_monitored(conn, THIS_MONTH, days=5)
    _insert_article(conn, title="Robi rebrands Airtel as Cirkle", llm_score=None)
    conn.commit()
    rows = client.get("/dashboard/long/telecom").json()["peers"]["rows"]
    airtel = next(r for r in rows if r["key"] == "airtel")
    assert airtel["items"] == 1
    assert airtel["mean"] is None
    assert airtel["critical"] == 0


def test_rows_are_ordered_by_severity_not_volume(client):
    """The design argument, pinned: a loud-but-mild axis must not
    outrank a quiet-but-severe one."""
    conn = client.conn
    _mark_monitored(conn, THIS_MONTH, days=5)
    for _ in range(6):
        _insert_article(conn, title="BTRC routine circular", llm_score=3.0, llm_reason="routine")
    _insert_article(conn, title="Submarine cable bandwidth failure", llm_score=9.0, llm_reason="severe")
    conn.commit()
    rows = client.get("/dashboard/long/telecom").json()["regulatory"]["rows"]
    scored = [r for r in rows if r["mean"] is not None]
    assert scored == sorted(scored, key=lambda r: -r["mean"])
    assert [r["mean"] for r in rows if r["mean"] is None] == [
        r["mean"] for r in rows[len(scored) :]
    ], "unscored rows sort last"


def test_expanded_items_are_capped_and_flagged_as_truncated(client):
    conn = client.conn
    _mark_monitored(conn, THIS_MONTH, days=5)
    for i in range(5):
        _insert_article(conn, title=f"BTRC enforcement action {i}", llm_score=9.0, llm_reason="why")
    conn.commit()
    row = next(
        r for r in client.get("/dashboard/long/telecom").json()["regulatory"]["rows"] if r["key"] == "btrc"
    )
    assert row["critical"] == 5
    assert len(row["crit"]) == 3
    assert row["criticalTruncated"] is True


def test_reason_is_carried_verbatim_and_absence_is_not_invented(client):
    conn = client.conn
    _mark_monitored(conn, THIS_MONTH, days=5)
    _insert_article(conn, title="BTRC fines GP", llm_score=9.0, llm_reason="Direct regulatory penalty.")
    # `articles.llm_reason` is NOT NULL, so an unrecorded reason is the
    # empty string rather than NULL. That is the state the row has to
    # render honestly — it must not borrow the neighbouring reason or
    # write one of its own.
    _insert_article(conn, title="BTRC spectrum notice", llm_score=9.0, llm_reason="")
    conn.commit()
    row = next(
        r for r in client.get("/dashboard/long/telecom").json()["regulatory"]["rows"] if r["key"] == "btrc"
    )
    reasons = {i["title"]: i["reason"] for i in row["crit"]}
    assert reasons["BTRC fines GP"] == "Direct regulatory penalty."
    assert reasons["BTRC spectrum notice"] == ""


# ── regressions ──────────────────────────────────────────────────────


def test_initiatives_never_leak_past_the_window_or_score_floor(client):
    """REGRESSION: AND/OR precedence in the initiatives query.

    An unscored article for a non-first operator used to bypass both the
    window and the score floor, because `A AND B AND c1 OR c2` parses as
    `(A AND B AND c1) OR c2`.
    """
    conn = client.conn
    _mark_monitored(conn, THIS_MONTH, days=5)
    # Grameenphone is the first operator clause; Banglalink is not.
    _insert_article(conn, title="Grameenphone launches a thing", llm_score=9.0, llm_reason="r")
    _insert_article(conn, title="Banglalink unscored item", llm_score=None)
    _insert_article(conn, title="Banglalink very old item", llm_score=9.0, llm_reason="r", day=date(2000, 1, 1))
    _insert_article(conn, title="Banglalink low-scoring item", llm_score=1.0, llm_reason="r")
    conn.commit()

    initiatives = client.get("/dashboard/long/telecom").json()["initiatives"]
    titles = {i["title"] for i in initiatives}
    assert "Banglalink unscored item" not in titles
    assert "Banglalink very old item" not in titles
    assert "Banglalink low-scoring item" not in titles
    assert all(i["score"] is not None and i["score"] >= 4.0 for i in initiatives)


def test_initiatives_show_a_spread_of_operators_not_one_loud_story(client):
    """REGRESSION: a global top-N returned the same launch six times.

    Five outlets carrying one Grameenphone story outranked every other
    operator, so a board about peers showed no peers.
    """
    conn = client.conn
    _mark_monitored(conn, THIS_MONTH, days=5)
    for i in range(5):
        _insert_article(conn, title=f"Grameenphone launches 1B1I ({i})", llm_score=10.0, llm_reason="r")
    _insert_article(conn, title="Banglalink seeks towerless approval", llm_score=8.0, llm_reason="r")
    _insert_article(conn, title="Teletalk package update", llm_score=6.0, llm_reason="r")
    conn.commit()

    initiatives = client.get("/dashboard/long/telecom").json()["initiatives"]
    gp_items = [i for i in initiatives if "Grameenphone" in i["title"]]
    assert len(gp_items) == 1, "one row per operator, not one row per outlet"
    assert any("Banglalink" in i["title"] for i in initiatives)
    assert any("Teletalk" in i["title"] for i in initiatives)


def test_competitive_line_reads_the_market_window_not_the_news_window(client):
    """REGRESSION: the market line inherited the news window.

    With collection running for one month it compared the last two
    closes instead of the last twelve, and reported a gap far smaller
    than the real one. The exchange keeps publishing when the collector
    does not, and this line has to say so.
    """
    conn = client.conn
    # Only the CURRENT month is monitored — the news window is one month.
    _mark_monitored(conn, THIS_MONTH, days=5)
    # Twelve months of prices: we fall 20%, the peer holds flat.
    us, peer = [], []
    for i in range(12):
        m = date(THIS_MONTH.year, THIS_MONTH.month, 1) - timedelta(days=30 * (11 - i))
        us.append((m.replace(day=15), 100.0 - i * 2.0))
        peer.append((m.replace(day=15), 50.0))
    _seed_market(conn, "GP", us)
    _seed_market(conn, "ROBI", peer)
    conn.commit()

    line = next(ln for ln in client.get("/dashboard/long/risk").json()["lines"] if ln["key"] == "competitive")
    # Reading only the monitored month would show roughly −2pts; reading
    # the full year shows roughly −20.
    gap = float(line["value"].replace("−", "-").replace("+", ""))
    assert gap < -15, f"expected the twelve-month gap, got {gap}"


# ── auth ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("tab", ["risk", "market", "economy", "telecom"])
def test_tabs_require_a_session(monkeypatch, offline_macro, tab):
    import app.api.routers.dashboard_long as mod

    with migrated_schema() as conn:
        monkeypatch.setattr(mod, "get_connection", lambda: contextlib.nullcontext(conn))
        with TestClient(create_app()) as c:
            assert c.get(f"/dashboard/long/{tab}").status_code == 401


def test_watchlist_writes_require_an_admin(monkeypatch, offline_macro):
    import app.api.routers.dashboard_long as mod

    with migrated_schema() as conn:
        monkeypatch.setattr(mod, "get_connection", lambda: contextlib.nullcontext(conn))
        app = create_app()
        app.dependency_overrides[get_current_user] = lambda: AuthenticatedUser(
            id=2, email="a@b.c", role="analyst", display_name="A", session_id=2
        )
        with TestClient(app) as c:
            assert c.post("/watchlist/preview", json={"enTerms": ["x"]}).status_code == 403
            assert c.post("/watchlist", json={"groupKey": "mfs", "label": "X"}).status_code == 403


# ── watchlist add-flow ───────────────────────────────────────────────


def test_preview_proposes_the_guard_for_the_chosen_board(client):
    conn = client.conn
    # নগদ is the ordinary Bangla word for cash: two of these three are
    # not the company, and only one carries a payments cue.
    _insert_article(conn, title="নগদ টাকা ছিনতাই হয়েছে")
    _insert_article(conn, title="ফল আমদানিতে শতভাগ নগদ মার্জিন")
    _insert_article(conn, title="নগদ ও বিকাশের মোবাইল ব্যাংকিং সার্ভিস চার্জ")
    conn.commit()

    r = client.post("/watchlist/preview", json={"bnTerms": ["নগদ"], "groupKey": "mfs"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["guard"] is not None and d["guard"]["key"] == "mfs"
    assert d["unguardedTotal"] == 3
    assert d["guardedTotal"] is not None and d["guardedTotal"] < d["unguardedTotal"]


def test_create_then_delete_a_custom_entity_round_trips(client):
    r = client.post(
        "/watchlist",
        json={"groupKey": "mfs", "label": "Upay", "note": "Third wallet", "enTerms": ["upay"]},
    )
    assert r.status_code == 201, r.text
    created = r.json()
    assert created["custom"] is True

    entities = client.get("/dashboard/long/telecom").json()["watchlist"]["entities"]
    assert any(e["label"] == "Upay" and e["custom"] for e in entities)

    assert client.delete(f"/watchlist/{created['id']}").status_code == 204
    entities = client.get("/dashboard/long/telecom").json()["watchlist"]["entities"]
    assert not any(e["label"] == "Upay" for e in entities)


def test_deleting_a_missing_entity_is_a_404_not_a_500(client):
    assert client.delete("/watchlist/999999").status_code == 404


def test_a_regex_metacharacter_in_a_term_is_refused_or_neutralised(client):
    """A term is user input that becomes a regex operand.

    Whatever the service does with it, the endpoint must not 500 and the
    pattern must not match everything.
    """
    conn = client.conn
    _insert_article(conn, title="Completely unrelated headline")
    conn.commit()
    r = client.post("/watchlist/preview", json={"enTerms": ["a.*b"], "groupKey": "operators"})
    assert r.status_code in (200, 422), r.text
    if r.status_code == 200:
        assert r.json()["unguardedTotal"] == 0


def test_creating_an_entity_that_collides_with_a_code_defined_key_is_refused(client):
    r = client.post("/watchlist", json={"groupKey": "operators", "label": "Grameenphone", "enTerms": ["gp"]})
    assert r.status_code == 422, r.text


# ── hardcoding guards ────────────────────────────────────────────────


def test_the_new_badge_is_derived_from_history_not_from_an_entity_name(client):
    """REGRESSION: `isNew` was `group == "mfs" or key == "airtel"`.

    That named the entities added in one particular week and would have
    gone on calling them new forever. The mark means "no durable history
    behind this row yet", which is a property of the data, so it has to
    clear itself once the rollup covers the row.
    """
    conn = client.conn
    _mark_monitored(conn, THIS_MONTH, days=5)
    _insert_article(conn, title="BTRC fines an operator", llm_score=9.0, llm_reason="r")
    conn.commit()

    rows = {r["key"]: r for r in client.get("/dashboard/long/telecom").json()["regulatory"]["rows"]}
    # No rollup rows exist in this schema at all, so every row is "new".
    assert rows["btrc"]["isNew"] is True

    conn.execute(
        "INSERT INTO rollup_monthly_watch (month, group_key, entity, article_count, scored_count, "
        "high_count, score_sum) VALUES (%s, 'regulatory', 'btrc', 4, 3, 2, 24)",
        (THIS_MONTH,),
    )
    conn.commit()
    rows = {r["key"]: r for r in client.get("/dashboard/long/telecom").json()["regulatory"]["rows"]}
    assert rows["btrc"]["isNew"] is False, "a row with rollup history is no longer new"
    # And the counts now come from the rollup, not from live articles.
    assert rows["btrc"]["items"] == 4
    assert rows["btrc"]["mean"] == 8.0


def test_political_line_says_so_when_its_section_is_not_configured(client, monkeypatch):
    """REGRESSION: the section name was hardcoded.

    `keywords.section` is admin-creatable, so an admin who renames or
    removes the Political section used to get a line reporting zero
    coverage forever instead of an explanation.
    """
    import app.services.risklines as rl

    conn = client.conn
    _mark_monitored(conn, THIS_MONTH, days=5)
    conn.commit()
    monkeypatch.setattr(rl, "get_risk_sections", lambda c: ("Geopolitical", "Macroeconomy"))

    line = next(ln for ln in client.get("/dashboard/long/risk").json()["lines"] if ln["key"] == "political")
    assert line["direction"] == "none"
    assert "not configured" in line["evidence"] or "nothing to read" in line["evidence"]
