"""Tests for the general app Dashboard (frontend-contract.md §3.1/§3.2):
GET /dashboard/today, GET /dashboard/trends. Distinct from ADMIN-API's
/dashboard/storage/* in the same router file.

Service-layer-shaped tests run against a real, migrated Postgres schema
(app.db.testing.migrated_schema) — same pattern as tests/test_admin_*.py.
"""

from __future__ import annotations

import contextlib
import itertools
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.api.deps import AuthenticatedUser, get_current_user
from app.api.routers.dashboard import _add_months, _month_start, _today_dhaka
from app.api.schemas.core import _reset_risk_sections_cache_for_tests
from app.db import queries
from app.db.testing import migrated_schema, pg_available
from app.main import create_app
from app.services import admin as admin_service

pytestmark = pytest.mark.skipif(not pg_available(), reason="no reachable Postgres (set TEST_DATABASE_URL)")

USER = AuthenticatedUser(id=1, email="analyst@grameenphone.com", role="analyst", display_name="Analyst", session_id=1)

_url_seq = itertools.count()


def _insert_article(conn, **fields):
    if fields.get("review_status") == "approved" and "reviewed_at" not in fields:
        fields["reviewed_at"] = datetime.now(timezone.utc)
    defaults = {
        "url": f"https://x.test/article-{next(_url_seq)}",
        "url_norm": "",
        "source": "Prothom Alo",
        "title": "Test headline",
        "day": date.today(),
        "sections": "",
        "keywords": "",
        "review_status": "pending",
    }
    defaults.update(fields)
    columns = list(defaults.keys())
    placeholders = ", ".join(f"%({c})s" for c in columns)
    row = conn.execute(
        f"INSERT INTO articles ({', '.join(columns)}) VALUES ({placeholders}) RETURNING id", defaults
    ).fetchone()
    return row["id"]


@pytest.fixture
def dash_client(monkeypatch: pytest.MonkeyPatch):
    import app.api.routers.dashboard as dashboard_router_module

    with migrated_schema() as conn:
        monkeypatch.setattr(dashboard_router_module, "get_connection", lambda: contextlib.nullcontext(conn))
        # RISK_SECTIONS is now a cached-per-process query (app.api.schemas.core.get_risk_sections)
        # instead of a static tuple -- reset it so one test's cached
        # section list can't leak into the next test's assertions, since
        # every test here gets its own freshly migrated schema but the
        # cache is a plain process-global.
        _reset_risk_sections_cache_for_tests()
        app = create_app()
        app.dependency_overrides[get_current_user] = lambda: USER
        with TestClient(app) as client:
            yield client, conn


def test_today_unauthenticated_returns_401() -> None:
    app = create_app()
    with TestClient(app) as client:
        resp = client.get("/dashboard/today")
    assert resp.status_code == 401


def test_trends_unauthenticated_returns_401() -> None:
    app = create_app()
    with TestClient(app) as client:
        resp = client.get("/dashboard/trends")
    assert resp.status_code == 401


def test_today_counts_reflect_real_rows(dash_client) -> None:
    client, conn = dash_client
    today = date.today()
    _insert_article(
        conn, day=today, source="Prothom Alo", sections="Macroeconomy",
        keywords="inflation", review_status="pending", llm_score=9,
    )
    _insert_article(conn, day=today, source="Daily Star", sections="", keywords="", review_status="filtered_out")
    _insert_article(
        conn, day=today, source="Daily Star", sections="Telco/regulatory",
        keywords="spectrum", review_status="approved", llm_score=6,
    )
    conn.commit()

    resp = client.get("/dashboard/today")
    assert resp.status_code == 200
    data = resp.json()
    assert data["kpis"][0]["value"] == "3"  # fetched today: all 3 rows
    assert data["kpis"][1]["value"] == "2"  # matched: excludes the filtered_out row
    assert len(data["funnel"]) == 4
    assert len(data["days14"]) == 14
    assert data["days14"][-1]["date"] == today.isoformat()
    section_labels = {s["label"] for s in data["bySection"]}
    assert section_labels == {"Geopolitical", "Macroeconomy", "Telco/regulatory", "Political", "Others"}
    by_section = {s["label"]: s["count"] for s in data["bySection"]}
    assert by_section["Macroeconomy"] == 1
    assert by_section["Telco/regulatory"] == 1
    assert {s["label"] for s in data["topSources"]} == {"Prothom Alo", "Daily Star"}


def test_pending_count_excludes_hidden_duplicate_rows(dash_client) -> None:
    """`pending_count` (KPI "Awaiting review" / `pendingCount`) must apply
    the same `COALESCE(is_primary, TRUE) = TRUE` filter the review queue's
    own list endpoint (app.api.routers.review) uses -- otherwise the
    badge/KPI counts non-primary duplicate-outlet rows a user can never
    see or act on individually in the queue, overcounting vs. what the
    queue actually shows.
    """
    client, conn = dash_client
    today = date.today()
    _insert_article(conn, day=today, review_status="pending", dup_group=1, is_primary=True)
    _insert_article(conn, day=today, review_status="pending", dup_group=1, is_primary=False)
    _insert_article(conn, day=today, review_status="pending", dup_group=1, is_primary=False)
    _insert_article(conn, day=today, review_status="pending")  # is_primary NULL -> counted (COALESCE TRUE)
    conn.commit()

    resp = client.get("/dashboard/today")
    assert resp.status_code == 200
    data = resp.json()
    assert data["pendingCount"] == 2
    awaiting = next(k for k in data["kpis"] if k["label"] == "Awaiting review")
    assert awaiting["value"] == "2"


def test_today_relmix_bands_by_score(dash_client) -> None:
    client, conn = dash_client
    today = date.today()
    _insert_article(conn, day=today, sections="Others", review_status="pending", llm_score=9)  # red
    _insert_article(conn, day=today, sections="Others", review_status="pending", llm_score=7)  # yellow
    _insert_article(conn, day=today, sections="Others", review_status="pending", llm_score=5)  # green
    _insert_article(conn, day=today, sections="Others", review_status="pending", llm_score=2)  # magenta
    conn.commit()

    resp = client.get("/dashboard/today")
    assert resp.status_code == 200
    rel = {r["key"]: r["count"] for r in resp.json()["relMix"]}
    assert rel == {"red": 1, "yellow": 1, "green": 1, "magenta": 1}


def test_bysection_reflects_a_brand_new_admin_created_section(dash_client) -> None:
    """The hardcoded-sections bug fix: RISK_SECTIONS used to be a static
    5-value tuple, so a section an admin creates via the Keywords screen
    (KEYWORDS-CONFIG — `keywords.section` was already unconstrained TEXT)
    would never show up in the dashboard's by-section breakdown. Creating
    a keyword in a brand-new section here, via the real admin service
    call the Keywords API route itself uses, must make that section
    appear in GET /dashboard/today's bySection with no further code
    change.
    """
    client, conn = dash_client
    admin_service.create_keyword(conn, section="Competitors", term_en="Robi", enabled=True)
    conn.commit()

    resp = client.get("/dashboard/today")
    assert resp.status_code == 200
    section_labels = {s["label"] for s in resp.json()["bySection"]}
    assert "Competitors" in section_labels
    # The original 5 fixed sections are still real keyword-derived
    # sections too (not silently dropped by the fix), since
    # list_keyword_sections includes "Uncategorized" for any keyword with
    # no section at all, not a hardcoded default list.


def test_today_kpis_include_real_daily_sparkline_vals(dash_client) -> None:
    client, conn = dash_client
    today = date.today()
    yesterday = today - timedelta(days=1)
    _insert_article(conn, day=today, review_status="pending")
    _insert_article(conn, day=today, review_status="pending")
    _insert_article(conn, day=yesterday, review_status="pending")
    conn.commit()

    resp = client.get("/dashboard/today")
    assert resp.status_code == 200
    kpis = resp.json()["kpis"]
    fetched_kpi = kpis[0]
    assert len(fetched_kpi["vals"]) == 14
    assert fetched_kpi["vals"][-1] == 2  # today
    assert fetched_kpi["vals"][-2] == 1  # yesterday
    # "Awaiting review" is a point-in-time queue depth, not a real daily
    # series this app retains history for -- must stay empty, not faked.
    assert kpis[2]["label"] == "Awaiting review"
    assert kpis[2]["vals"] == []


def test_trends_reads_rollups_not_raw_articles(dash_client) -> None:
    client, conn = dash_client
    today = _today_dhaka()
    since = _add_months(_month_start(today), -11)
    months = [_add_months(since, i) for i in range(12)]

    for m in months:
        queries.upsert_rollup_monthly_section(conn, m, "Macroeconomy", fetched_delta=10, matched_delta=5)
    conn.execute(
        "INSERT INTO macro_indicator_values (indicator, month, value) VALUES (%s, %s, %s)",
        ("taka_usd", months[-1], 123.4),
    )
    # A theme present in 9 of the 12 months qualifies as "standing" (>=9).
    for m in months[:9]:
        queries.upsert_rollup_monthly_theme(conn, m, "Taka depreciation", count_delta=4, intensity=0.5)
    conn.commit()

    resp = client.get("/dashboard/trends")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["trendCards"]) == 3
    taka_card = next(c for c in data["trendCards"] if c["label"] == "Taka / USD")
    assert taka_card["vals"][-1] == pytest.approx(123.4)
    assert len(data["months12"]) == 12
    assert data["months12"][-1]["segments"].get("Macroeconomy") == 5
    assert any(h["label"] == "Taka depreciation" for h in data["heatRows"])
    standing_names = [s["name"] for s in data["standing"]]
    assert "Taka depreciation" in standing_names
    entry = next(s for s in data["standing"] if s["name"] == "Taka depreciation")
    assert entry["months"] == 9
    assert entry["count"] == 36


def test_trends_relmix_trend_and_top_sources_are_real_rollup_reads(dash_client) -> None:
    client, conn = dash_client
    today = _today_dhaka()
    since = _add_months(_month_start(today), -11)
    months = [_add_months(since, i) for i in range(12)]
    last_month = months[-1]

    queries.upsert_rollup_monthly_score_band(conn, last_month, "high", count_delta=3)
    queries.upsert_rollup_monthly_score_band(conn, last_month, "medium", count_delta=1)
    for m in months:
        queries.upsert_rollup_monthly_source(conn, m, "Prothom Alo", matched_delta=2)
        queries.upsert_rollup_monthly_source(conn, m, "Daily Star", matched_delta=1)
    conn.commit()

    resp = client.get("/dashboard/trends")
    assert resp.status_code == 200
    data = resp.json()

    assert len(data["relMixTrend"]) == 12
    last_point = data["relMixTrend"][-1]
    assert last_point["total"] == 4
    assert last_point["highShare"] == pytest.approx(0.75)
    assert all(p["total"] == 0 for p in data["relMixTrend"][:-1])

    top_sources = {s["label"]: s["count"] for s in data["topSources"]}
    assert top_sources["Prothom Alo"] == 24  # 2/month x 12 months
    assert top_sources["Daily Star"] == 12


def test_macro_card_says_no_data_instead_of_showing_a_zero(dash_client, monkeypatch) -> None:
    """A failed live fetch with nothing stored must not print the
    gap-fill 0.0 as a reading.

    `vals` fills missing months with 0.0 so the sparkline has twelve
    points. `macro_indicator_values` is empty in this deployment, so
    when the live fetch also failed the card rendered "Inflation 0.0%"
    under the caption "showing the last known figure" — a last known
    figure that does not exist. An inflation rate of 0.0 is plausible
    enough that nobody looks twice, which makes it worse than a blank.
    """
    client, conn = dash_client
    conn.commit()

    import app.api.routers.dashboard as dash_module

    monkeypatch.setattr(dash_module.macro_service, "fetch_usd_bdt", lambda: None)
    monkeypatch.setattr(dash_module.macro_service, "fetch_inflation", lambda: None)
    monkeypatch.setattr(dash_module.macro_service, "fetch_brent", lambda _key: None)

    data = client.get("/dashboard/trends").json()
    inflation = next(c for c in data["trendCards"] if c["label"] == "Inflation")

    assert inflation["status"] == "no_data"
    assert "last known figure" not in (inflation["note"] or "")
    assert "no earlier figure is stored" in inflation["note"]


def test_macro_card_shows_the_stored_figure_when_one_exists(dash_client, monkeypatch) -> None:
    """The other half of the same branch: when a stored figure IS
    present, falling back to it is correct and the card should say so
    rather than claiming no data.
    """
    client, conn = dash_client
    this_month = _month_start(_today_dhaka())
    conn.execute(
        "INSERT INTO macro_indicator_values (indicator, month, value) VALUES (%s, %s, %s)",
        ("inflation", this_month, 9.7),
    )
    conn.commit()

    import app.api.routers.dashboard as dash_module

    monkeypatch.setattr(dash_module.macro_service, "fetch_inflation", lambda: None)

    data = client.get("/dashboard/trends").json()
    inflation = next(c for c in data["trendCards"] if c["label"] == "Inflation")

    assert inflation["status"] == "rollup"
    assert inflation["vals"][-1] == pytest.approx(9.7)
    assert "last stored figure" in inflation["note"]


# ── Long term: the CRO blocks (migrations/0021) ─────────────────────────
#
# The reader of /dash/long is Grameenphone's ERM function. What that
# reader needs from a 12-month view is (a) whether the window was even
# observed, (b) what the regulator and the peer operators have been
# doing, and (c) whether anything is rising on volume AND severity at
# once. The tests below pin the honesty properties of those blocks,
# because every one of them is a place where a plausible-looking number
# could be printed instead of an absence.


def _set_coverage(conn, month, *, collected_days: int, articles: int = 0) -> None:
    conn.execute(
        "INSERT INTO rollup_monthly_coverage (month, article_count, matched_count, scored_count, "
        "collected_count, collected_days) VALUES (%s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (month) DO UPDATE SET article_count = EXCLUDED.article_count, "
        "collected_days = EXCLUDED.collected_days",
        (month, articles, articles, articles, articles if collected_days else 0, collected_days),
    )


def _set_watch(conn, month, entity, *, count: int, scored: int = 0, high: int = 0, score_sum: float = 0.0) -> None:
    conn.execute(
        "INSERT INTO rollup_monthly_watch (month, group_key, entity, article_count, matched_count, "
        "scored_count, high_count, score_sum) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (month, group_key, entity) DO UPDATE SET article_count = EXCLUDED.article_count, "
        "scored_count = EXCLUDED.scored_count, high_count = EXCLUDED.high_count, score_sum = EXCLUDED.score_sum",
        (month, "regulatory", entity, count, count, scored, high, score_sum),
    )


def test_trends_marks_unmonitored_months_instead_of_drawing_them_as_zero(dash_client) -> None:
    """`articles.day` is a PUBLICATION date, so a month before the
    collector existed still carries backdated articles. Drawn as an
    ordinary bar that reads as "risk coverage was flat all year then
    exploded", which is a fact about when monitoring started.
    """
    client, conn = dash_client
    today = _today_dhaka()
    since = _add_months(_month_start(today), -11)
    months = [_add_months(since, i) for i in range(12)]

    # Ten months of backdated leftovers, two months actually collected.
    for m in months[:10]:
        _set_coverage(conn, m, collected_days=0, articles=4)
    _set_coverage(conn, months[10], collected_days=9, articles=400)
    _set_coverage(conn, months[11], collected_days=4, articles=200)
    for m in months:
        queries.upsert_rollup_monthly_section(conn, m, "Telco/regulatory", fetched_delta=4, matched_delta=4)
    conn.commit()

    data = client.get("/dashboard/trends").json()

    assert data["monitoredMonths"] == 2
    assert [c["monitored"] for c in data["coverage"]] == [False] * 10 + [True, True]
    # The backdated months still report their real article count. They
    # are not erased, only marked -- erasing them would be its own lie.
    assert data["coverage"][0]["articles"] == 4
    assert data["coverage"][0]["collectedDays"] == 0
    assert [m["monitored"] for m in data["months12"]] == [False] * 10 + [True, True]
    assert "not monitored rather than as zero" in data["windowNote"]
    assert "13 days across 2" in data["windowNote"]


def test_trends_says_so_when_no_month_was_monitored(dash_client) -> None:
    """The empty-deployment case must not render as twelve quiet months."""
    client, conn = dash_client
    conn.commit()

    data = client.get("/dashboard/trends").json()

    assert data["monitoredMonths"] == 0
    assert "nothing below is a trend" in data["windowNote"]
    for group in data["watch"]:
        for row in group["rows"]:
            assert row["dir"] == "unknown"
            assert row["latestRate"] is None
            assert "No month in this window was monitored." == row["dirBasis"]


def test_trends_watch_direction_is_per_collection_day_not_raw_count(dash_client) -> None:
    """The collector ran 9 days one month and 4 the next in the real
    deployment. An unchanged raw count across those two months is a rise
    in coverage, and a chart comparing raw counts would call it flat.
    """
    client, conn = dash_client
    today = _today_dhaka()
    since = _add_months(_month_start(today), -11)
    months = [_add_months(since, i) for i in range(12)]

    _set_coverage(conn, months[10], collected_days=9, articles=400)
    _set_coverage(conn, months[11], collected_days=4, articles=200)
    # Identical raw counts, very different amounts of looking.
    _set_watch(conn, months[10], "btrc", count=11, scored=7, high=5, score_sum=58.0)
    _set_watch(conn, months[11], "btrc", count=11, scored=10, high=2, score_sum=59.0)
    conn.commit()

    data = client.get("/dashboard/trends").json()
    regulatory = next(g for g in data["watch"] if g["key"] == "regulatory")
    btrc = next(r for r in regulatory["rows"] if r["key"] == "btrc")

    assert btrc["latest"] == btrc["previous"] == 11  # raw counts identical
    assert btrc["latestRate"] == pytest.approx(2.75)  # 11 / 4 days
    assert btrc["prevRate"] == pytest.approx(1.22)  # 11 / 9 days
    assert btrc["dir"] == "up"
    # The arrow has to be checkable without leaving the screen.
    assert "2.75 per collection day" in btrc["dirBasis"]
    assert "11 over 4 days, then 11 over 9" in btrc["dirBasis"]


def test_trends_escalation_needs_both_volume_and_severity(dash_client) -> None:
    """Volume alone is not risk. A theme whose count doubled while its
    articles scored lower is noisier, not worse, and must not be badged.
    """
    client, conn = dash_client
    today = _today_dhaka()
    since = _add_months(_month_start(today), -11)
    months = [_add_months(since, i) for i in range(12)]

    _set_coverage(conn, months[10], collected_days=4, articles=400)
    _set_coverage(conn, months[11], collected_days=4, articles=400)

    # Volume up 4x, mean severity DOWN (10.0 -> 7.0). Not escalation.
    _set_watch(conn, months[10], "spectrum", count=4, scored=1, high=1, score_sum=10.0)
    _set_watch(conn, months[11], "spectrum", count=16, scored=8, high=4, score_sum=56.0)
    # Volume up AND mean severity up (5.0 -> 8.0). Escalation.
    _set_watch(conn, months[10], "btrc", count=4, scored=4, high=0, score_sum=20.0)
    _set_watch(conn, months[11], "btrc", count=8, scored=8, high=6, score_sum=64.0)
    conn.commit()

    data = client.get("/dashboard/trends").json()
    rows = {r["key"]: r for r in next(g for g in data["watch"] if g["key"] == "regulatory")["rows"]}

    assert rows["spectrum"]["dir"] == "up"
    assert rows["spectrum"]["escalating"] is False
    assert rows["btrc"]["escalating"] is True


def test_trends_severity_is_n_a_rather_than_zero_when_nothing_was_scored(dash_client) -> None:
    """A printed 0.0 on a 0 to 10 relevance scale reads as "assessed,
    and harmless" -- the opposite of "never assessed". Same defect the
    macro cards were already fixed for.
    """
    client, conn = dash_client
    today = _today_dhaka()
    since = _add_months(_month_start(today), -11)
    months = [_add_months(since, i) for i in range(12)]

    _set_coverage(conn, months[11], collected_days=4, articles=100)
    _set_watch(conn, months[11], "telecom_tax", count=2, scored=0, high=0, score_sum=0.0)
    conn.commit()

    data = client.get("/dashboard/trends").json()
    rows = {r["key"]: r for r in next(g for g in data["watch"] if g["key"] == "regulatory")["rows"]}
    tax = rows["telecom_tax"]

    assert tax["total"] == 2
    assert tax["avgScore"] is None
    assert tax["scored"] == 0
    assert "none scored by the AI stage yet" in tax["severityNote"]


def test_trends_reports_counts_hidden_in_unmonitored_months(dash_client) -> None:
    """The strip cannot draw a bar for an unmonitored month, so the
    total beside it would silently disagree with the picture. The gap is
    reported rather than left for the reader to trip over.
    """
    client, conn = dash_client
    today = _today_dhaka()
    since = _add_months(_month_start(today), -11)
    months = [_add_months(since, i) for i in range(12)]

    _set_coverage(conn, months[2], collected_days=0, articles=5)
    _set_coverage(conn, months[11], collected_days=4, articles=100)
    _set_watch(conn, months[2], "sim_rules", count=7, scored=7, high=1, score_sum=49.0)
    _set_watch(conn, months[11], "sim_rules", count=2, scored=1, high=1, score_sum=8.0)
    conn.commit()

    data = client.get("/dashboard/trends").json()
    rows = {r["key"]: r for r in next(g for g in data["watch"] if g["key"] == "regulatory")["rows"]}
    sim = rows["sim_rules"]

    assert sim["total"] == 9
    assert sim["backdated"] == 7  # published before monitoring began
    assert sim["monthsPresent"] == 1  # of the monitored months only


def test_trends_relmix_marks_unmonitored_months(dash_client) -> None:
    """The sharpest version of the same problem: this series is a SHARE,
    so one backdated article scoring 9 in an unmonitored month draws a
    full-height 100% bar next to a real month's 28%.
    """
    client, conn = dash_client
    today = _today_dhaka()
    since = _add_months(_month_start(today), -11)
    months = [_add_months(since, i) for i in range(12)]

    _set_coverage(conn, months[0], collected_days=0, articles=1)
    _set_coverage(conn, months[11], collected_days=4, articles=100)
    queries.upsert_rollup_monthly_score_band(conn, months[0], "high", count_delta=1)
    queries.upsert_rollup_monthly_score_band(conn, months[11], "high", count_delta=28)
    queries.upsert_rollup_monthly_score_band(conn, months[11], "low", count_delta=72)
    conn.commit()

    data = client.get("/dashboard/trends").json()
    points = data["relMixTrend"]

    # The unmonitored month genuinely computes to 100%, and that is
    # exactly why it must be flagged rather than drawn.
    assert points[0]["highShare"] == pytest.approx(1.0)
    assert points[0]["monitored"] is False
    assert points[11]["highShare"] == pytest.approx(0.28)
    assert points[11]["monitored"] is True


def test_trends_standing_theme_note_uses_months_with_data_as_denominator(dash_client) -> None:
    """"Present in 2 of the last 12 months" reads as a theme that faded.
    When only 2 months carry any theme data, the honest denominator is 2.
    """
    client, conn = dash_client
    today = _today_dhaka()
    since = _add_months(_month_start(today), -11)
    months = [_add_months(since, i) for i in range(12)]

    for m in months[10:]:
        queries.upsert_rollup_monthly_theme(conn, m, "Spectrum repricing", count_delta=6, intensity=0.9)
    conn.commit()

    data = client.get("/dashboard/trends").json()
    entry = next(s for s in data["standing"] if s["name"] == "Spectrum repricing")

    assert entry["note"] == "Present in 2 of the 2 months with theme data."
