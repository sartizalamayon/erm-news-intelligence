"""Tests for the general app Dashboard (frontend-contract.md §3.1/§3.2):
GET /dashboard/today. Distinct from ADMIN-API's /dashboard/storage/* in
the same router file, and from the four /dashboard/long/* tabs
(tests/test_dashboard_long.py).

The long-term half of this file went with `GET /dashboard/trends`
itself — see the note at the foot of app/api/routers/dashboard.py. The
honesty properties those tests existed for (an absence must never be
drawn as a measured zero; a severity nobody assessed must read "n/a")
are pinned against the four endpoints that replaced it, in
tests/test_dashboard_long.py — not by porting the old assertions
across, since those tabs answer the "were we even watching" question
with a window derived from real coverage rather than with a per-month
`monitored` flag.

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
from app.api.schemas.core import _reset_risk_sections_cache_for_tests
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


def test_the_removed_trends_route_is_gone_rather_than_serving_a_fabricated_series() -> None:
    """`GET /dashboard/trends` used to gap-fill a twelve-month macro
    series out of `macro_indicator_values`, a table nothing writes:
    eleven zeroes and one live figure, for each of Taka/USD, inflation
    and Brent. The shipped frontend moved to the four /dashboard/long/*
    tabs and stopped calling it, so what remained was fabricated data
    behind an auth cookie. It is deleted, not merely unused.
    """
    app = create_app()
    with TestClient(app) as client:
        assert client.get("/dashboard/trends").status_code == 404
    # Not every entry in `app.routes` is a route with a path -- an
    # included sub-router appears here too, and has no `.path` at all.
    # Ask for the attribute rather than assuming it, so this asserts on
    # the real path set instead of dying on the first router.
    paths = {p for p in (getattr(route, "path", None) for route in app.routes) if p}
    assert "/dashboard/trends" not in paths, "the route object is still mounted, only unreachable"


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
    # This asserted the five names {"Geopolitical", "Macroeconomy",
    # "Telco/regulatory", "Political", "Others"} against a keywords
    # table that is EMPTY in this fixture -- i.e. it asserted the
    # hardcoded fallback, which is how the suite came to pin fabricated
    # data on the product's primary screen rather than catch it. No
    # section is configured here, so the only labels that may appear are
    # the ones today's articles actually carry.
    section_labels = {s["label"] for s in data["bySection"]}
    assert section_labels == {"Macroeconomy", "Telco/regulatory"}
    by_section = {s["label"]: s["count"] for s in data["bySection"]}
    assert by_section["Macroeconomy"] == 1
    assert by_section["Telco/regulatory"] == 1
    assert {s["label"] for s in data["topSources"]} == {"Prothom Alo", "Daily Star"}


def test_a_fresh_install_reports_no_sections_rather_than_five_invented_ones(dash_client) -> None:
    """Nothing configured and nothing collected must render as nothing.

    `keywords` is empty on a fresh install -- no migration seeds it --
    and `get_risk_sections` used to answer that with a fixed five-name
    tuple. The card then opened on five sections nobody had configured,
    each reading 0, indistinguishable from real ones.
    """
    client, _conn = dash_client
    data = client.get("/dashboard/today").json()
    assert data["bySection"] == []


def test_a_configured_section_still_shows_at_zero_when_it_matched_nothing(dash_client) -> None:
    """The other half of the union: "configured and quiet" has to stay
    distinguishable from "not configured at all", so a section with no
    articles today is present at 0 rather than absent.
    """
    client, conn = dash_client
    conn.execute(
        "INSERT INTO keywords (term_en, term_bn, section, enabled) VALUES (%s, %s, %s, TRUE)",
        ("spectrum", "", "Telco/regulatory"),
    )
    conn.commit()
    _reset_risk_sections_cache_for_tests()

    data = client.get("/dashboard/today").json()
    by_section = {s["label"]: s["count"] for s in data["bySection"]}
    assert by_section == {"Telco/regulatory": 0}


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
