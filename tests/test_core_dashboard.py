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
