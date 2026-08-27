"""Every route, against a freshly migrated database with no data in it.

WHY THIS FILE EXISTS. Nearly every other test in this suite builds its
fixtures first and then calls the API, so the "no data yet" state — the
one the product is guaranteed to be in on the morning of the first
production deploy, and the only state EVERY installation passes through
— was the least-tested path in the codebase. Two fabricated fallbacks
had already been found and removed by hand before this sweep (a
hardcoded five-section list on `GET /dashboard/today`, and an
eleven-month macro series manufactured out of `0.0`); neither had a test
that would have caught them, because no test ever asked the API what it
says when it knows nothing.

The two rules this file enforces are the ones that matter on day one:

  1. NOTHING MAY 500. An empty table is a normal state, not an error.
  2. NOTHING MAY REPORT A FABRICATED VALUE. When there is no data, the
     honest answers are an empty list, a null, an em dash, or an
     explicit "not configured" — never an invented number, never a zero
     dressed up as a measurement, never a placeholder name presented as
     real configuration.

Rule 1 is enforced by sweeping every route the app publishes, rather
than by listing routes by hand, so a route added next month is covered
the day it is added and cannot quietly skip this check.

The sweep runs with the suite's autouse network guard active
(tests/conftest.py), which is deliberate and load-bearing: a fresh
deploy has no provider credentials stored and no outbound LLM call can
succeed, so "empty database" and "nothing reachable" are the SAME
morning. That combination is what exposed the `POST /prompts/suggest`
defect this file pins below — it was the one route that answered the
day-one state with an opaque 500.
"""

from __future__ import annotations

import contextlib
import importlib

import pytest
from fastapi.testclient import TestClient

from app.api.deps import AuthenticatedUser, get_current_user, require_admin
from app.db.testing import migrated_schema, pg_available
from app.main import create_app

pytestmark = pytest.mark.skipif(not pg_available(), reason="no reachable Postgres (set TEST_DATABASE_URL)")

ADMIN = AuthenticatedUser(id=1, email="admin@grameenphone.com", role="admin", display_name="Admin", session_id=1)

# Every router module that binds `get_connection` at module level; all of
# them are repointed at the one migrated test schema.
ROUTER_MODULES = (
    "auth", "brief", "dashboard", "dashboard_long", "export", "health",
    "keywords", "me", "prompts", "review", "search", "sources", "users",
)

# Path-parameter values. These deliberately name rows that DO NOT EXIST —
# that is the empty-database case for a detail route, and the honest
# answer is 404, never 500.
PATH_PARAMS = {
    "article_id": "1", "keyword_id": "1", "source_id": "1", "user_id": "1",
    "entity_id": "1", "section": "Telecom", "provider": "groq",
    "field": "guidance", "version": "1", "stage_key": "translate",
    "key_id": "1", "dup_id": "2",
}

# Minimal well-formed bodies, so a write route is exercised for real
# rather than bouncing off request validation before it reaches any SQL.
# A route absent from this map is called with no body; that still proves
# it answers 422 rather than 500.
BODIES = {
    ("POST", "/auth/otp/request"): {"email": "admin@grameenphone.com"},
    ("POST", "/auth/login"): {"email": "admin@grameenphone.com", "password": "Str0ng!Passw0rd"},
    ("POST", "/brief/draft"): {"sections": []},
    ("POST", "/export/presets"): {"name": "preset", "filter": "topn"},
    ("POST", "/keywords"): {"section": "Telecom", "term_en": "spectrum", "term_bn": "স্পেকট্রাম"},
    ("PATCH", "/keywords/{keyword_id}"): {"term_en": "spectrum"},
    ("PATCH", "/keywords/sections/{section}"): {"name": "Renamed"},
    ("PATCH", "/sources/{source_id}"): {"name": "Renamed source"},
    ("PATCH", "/articles/{article_id}"): {"status": "approved"},
    ("POST", "/articles/bulk"): {"ids": [], "action": "approve"},
    ("PUT", "/prompts/guidance"): {"content": "guidance text"},
    ("PUT", "/prompts/examples"): {"content": "examples text"},
    ("PUT", "/prompts/stages/{field}"): {"content": "stage text"},
    ("PATCH", "/prompts/providers/{provider}"): {"enabled": True},
    ("POST", "/watchlist/preview"): {"enTerms": ["grameenphone"], "bnTerms": []},
    ("POST", "/watchlist"): {"groupKey": "operators", "label": "L", "enTerms": ["gp"], "bnTerms": []},
}


@pytest.fixture(autouse=True)
def _day_one_process_env(monkeypatch: pytest.MonkeyPatch):
    """Pin the PROCESS environment to a realistic first-deploy state, so
    the sweep below varies only the thing it is about: an empty database.

    Two adjustments, each for a specific reason learned the hard way
    while writing this file.

    SECRET_KEY — tests/conftest.py deliberately blanks it (see its
    docstring), but then `POST /auth/otp/request` raises
    `ConfigurationError("SECRET_KEY is not configured -- cannot hash OTP
    codes.")` and answers 500. That is an unconfigured PROCESS, not an
    empty DATABASE, and production cannot reach it at all:
    `app.config.validate_production_settings` refuses to start without a
    key. Leaving it blank would park a permanent failure in this file
    for a condition that cannot happen in production, which is how a
    red test becomes background noise. One is configured here.

    The email provider credentials — blanked for the mirror-image
    reason. `app.config.Settings` reads `.env`, so a developer with real
    BREVO/RESEND credentials had `POST /auth/otp/request` treat the
    provider as configured, attempt a genuine HTTPS send, get stopped by
    the suite's network guard, and surface as a 500 — a result that
    depended entirely on whether the machine running the tests happened
    to have a `.env`. On the first morning of a real deployment no email
    provider is configured yet, which is precisely the state this file
    means to describe; blanked, the OTP route takes its documented
    console-fallback path (app/services/email.py) and answers honestly.

    NOTE, and worth a look separately from this file: with a provider
    genuinely configured but unreachable, that route does answer 500
    rather than a handled "could not send the code". That is an email
    OUTAGE, not an empty database, so it is out of scope here — but it
    is real, and it is the reason this fixture blanks rather than
    invents credentials.
    """
    from app.config import get_settings

    monkeypatch.setenv("SECRET_KEY", "test-only-secret-key-do-not-use-in-prod")
    for var in ("BREVO_API_KEY", "BREVO_FROM_EMAIL", "RESEND_API_KEY", "RESEND_FROM_EMAIL"):
        monkeypatch.setenv(var, "")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def empty_db_client(monkeypatch: pytest.MonkeyPatch):
    """An app whose every router talks to one freshly migrated, dataless
    schema. `app.api.deps` is repointed too, so the auth dependency does
    not reach for the real DSN when a route resolves its session.
    """
    import app.api.deps as deps_module

    modules = [importlib.import_module(f"app.api.routers.{name}") for name in ROUTER_MODULES]
    with migrated_schema() as conn:
        holder = lambda *args, **kwargs: contextlib.nullcontext(conn)  # noqa: E731
        for module in modules:
            if hasattr(module, "get_connection"):
                monkeypatch.setattr(module, "get_connection", holder)
        monkeypatch.setattr(deps_module, "get_connection", holder)

        app = create_app()
        app.dependency_overrides[get_current_user] = lambda: ADMIN
        app.dependency_overrides[require_admin] = lambda: ADMIN
        with TestClient(app, raise_server_exceptions=False) as client:
            yield client, conn


def _all_routes(client: TestClient) -> list[tuple[str, str]]:
    """Enumerated from the OpenAPI schema rather than by walking
    `app.routes`: this FastAPI wraps included routers in an
    `_IncludedRouter` object that exposes neither `.path` nor
    `.methods`, so walking the route list finds only the handful of
    routes declared directly on the app and silently misses every
    router-mounted one — which is all of them.
    """
    spec = client.app.openapi()  # type: ignore[attr-defined]
    routes = [
        (method.upper(), path)
        for path, operations in spec["paths"].items()
        for method in operations
        if method.upper() in ("GET", "POST", "PUT", "PATCH", "DELETE")
    ]
    return sorted(routes, key=lambda r: (r[1], r[0]))


def test_the_schema_really_is_empty_of_article_data(empty_db_client) -> None:
    """Guards the premise of every other test here. If a future
    migration started seeding articles, this file would go on passing
    while testing something entirely different, so the emptiness is
    asserted rather than assumed.

    `settings`, `users` and `provider_configs` are NOT asserted empty:
    migration 0001 seeds those deliberately (the storage ceiling, the
    allowed email domain, the first admin). That is configuration
    shipped with the schema, not data pretending to be measurements.
    """
    _, conn = empty_db_client
    for table in ("articles", "article_bodies", "keywords", "sources", "runs", "rollup_monthly_coverage"):
        count = conn.execute(f'SELECT count(*) AS n FROM "{table}"').fetchone()["n"]
        assert count == 0, f"{table} was expected to be empty but held {count} row(s)"


def test_no_route_returns_5xx_against_an_empty_database(empty_db_client) -> None:
    """RULE 1, swept across every published route.

    Reported as one collected list rather than failing on the first hit,
    so a run tells you everything that is broken instead of one thing at
    a time.
    """
    client, _ = empty_db_client
    failures = []
    for method, path in _all_routes(client):
        url = path
        for name, value in PATH_PARAMS.items():
            url = url.replace("{" + name + "}", value)
        body = BODIES.get((method, path))
        try:
            response = client.request(method, url, json=body) if body else client.request(method, url)
        except Exception as exc:  # noqa: BLE001 - a raise is as much a failure as a 500
            failures.append(f"{method} {path} RAISED {type(exc).__name__}: {exc}")
            continue
        if response.status_code >= 500:
            failures.append(f"{method} {path} -> {response.status_code} {response.text[:160]}")

    assert not failures, "routes returned 5xx against an empty database:\n  " + "\n  ".join(failures)


def test_suggest_refinement_explains_that_no_provider_is_configured(empty_db_client) -> None:
    """RULE 1, at the specific route that broke it.

    On the first morning of a deployment there is no provider credential
    stored yet — that is the normal state of the Prompts screen before
    an admin has visited the Providers tab. Clicking "Suggest a
    refinement" then raised `ConfigurationError` out of
    `app.services.llm.registry.build_client_for_feature`, and
    `app/api/error_handlers.py` turned it into `500
    {"error": "configuration_error"}` with the explanation written only
    to the server log.

    That handler is not wrong to exist — its own comment says a
    ConfigurationError is meant to fail the process at startup and
    "never reach a request handler". The bug is that this one genuinely
    does reach a request handler, on a state that is not a server fault
    at all: a thing the admin has not configured yet, on a screen where
    they can configure it. The registry already composes a precise,
    actionable message naming the feature, the providers checked and the
    remedy; it was simply being thrown away.

    Asserted on the SHAPE of the answer (4xx, and the remedy reaches the
    caller) rather than on exact wording, so improving the sentence does
    not fail the test.
    """
    client, _ = empty_db_client
    response = client.post("/prompts/suggest")
    assert response.status_code < 500, f"expected a client error, got {response.status_code}: {response.text}"
    assert 400 <= response.status_code < 500
    detail = str(response.json().get("detail", "")).lower()
    assert "provider" in detail, f"the reply must name what is missing; got {response.text}"
    assert "api key" in detail or "configured" in detail, (
        f"the reply must tell the admin what to do about it; got {response.text}"
    )


# ── RULE 2: no fabricated values ──────────────────────────────────────


def test_dashboard_today_invents_nothing(empty_db_client) -> None:
    """The screen the first fabricated fallback was found on.

    `bySection` returning a hardcoded five-name list, each reading 0, is
    the exact bug that was removed; an empty list is the honest answer
    when no keyword sections exist. The per-day series is allowed to be
    zeros because those ARE measurements — fourteen real dates, each
    genuinely holding no articles — but every date must be real and the
    count must never be invented.
    """
    client, _ = empty_db_client
    body = client.get("/dashboard/today").json()

    assert body["bySection"] == [], "sections nobody configured must not be invented"
    assert body["topSources"] == [], "sources that filed nothing must not be listed"
    assert body["pendingCount"] == 0
    assert body["approvedCount"] == 0
    assert all(kpi["value"] == "0" for kpi in body["kpis"]), "no KPI may report a non-zero count"
    assert all(kpi["delta"] == "" for kpi in body["kpis"]), (
        "a day that has not started has not underperformed; deltas must be suppressed, not computed"
    )
    assert all(day["count"] == 0 for day in body["days14"])
    assert len(body["days14"]) == 14, "the 14-day series must be real dates, not a padded placeholder"


def test_long_range_tabs_say_no_data_rather_than_showing_a_number(empty_db_client) -> None:
    """The second fabricated fallback lived here: an eleven-month macro
    series built out of `0.0`, which drew a flat line on a chart that
    should have been empty. With no rows and no reachable upstream, each
    macro card must carry an explicit no-data status and an empty
    series — not a zero, and not a stale figure.
    """
    client, _ = empty_db_client

    economy = client.get("/dashboard/long/economy").json()
    for card in economy["macro"]:
        # The three literals the code can actually emit for "nothing to
        # show", from app/api/routers/dashboard_long.py:
        #
        #   no_data       queried, nothing stored
        #   unconfigured  no OILPRICEAPI_KEY, so Brent was never asked for
        #
        # This asserted ("no_data", "not_configured") -- the second is a
        # spelling that appears nowhere in the codebase, and the real
        # "unconfigured" was missing. It passed locally only because the
        # author's .env carries an OILPRICEAPI_KEY, which sends Brent
        # down the `no_data` branch; the first CI run, with no .env and
        # no key, took the `unconfigured` branch and failed. A test whose
        # expected set is wrong AND whose environment hides it is worth
        # the extra comment.
        assert card["status"] in ("no_data", "unconfigured"), card
        assert card["value"] == "—", f"{card['key']} showed a value with no data behind it: {card}"
        assert card["series"] == [], f"{card['key']} manufactured a series: {card}"
        assert card["hasSeries"] is False
    for row in economy["board"]["rows"]:
        assert row["items"] == 0 and row["scored"] == 0 and row["critical"] == 0
        assert row["mean"] is None, "a mean over zero scored items must be null, never 0.0"

    market = client.get("/dashboard/long/market").json()
    assert market["instruments"] == [] and market["series"] == [] and market["months"] == []
    assert market["tradingDays"] == 0
    assert market["asOf"] == "", "an 'as of' date with no data behind it would be fabricated"

    risk = client.get("/dashboard/long/risk").json()
    for line in risk["lines"]:
        assert line["value"] == "—", f"risk line {line['key']} showed a number with no data: {line}"
        assert line["direction"] == "none"


def test_empty_collections_are_empty_lists_not_placeholders(empty_db_client) -> None:
    """The plain case, stated once so it cannot regress quietly: a
    collection endpoint over an empty table returns `[]`.
    """
    client, _ = empty_db_client
    for path in ("/keywords", "/keywords/sections", "/sources", "/health/sources",
                 "/health/runs", "/export/presets", "/prompts/candidates", "/auth/otp-failures"):
        response = client.get(path)
        assert response.status_code == 200, f"{path} -> {response.status_code}"
        assert response.json() == [], f"{path} returned placeholder content: {response.text[:200]}"


def test_search_and_review_report_a_true_zero_corpus(empty_db_client) -> None:
    """`corpusSize` is the one number on the Search screen that is a
    measurement rather than a match count, so it has to be honest in
    both directions: zero when the archive is empty, and not omitted.
    """
    client, _ = empty_db_client

    search = client.get("/search", params={"q": "spectrum"}).json()
    assert search == {"results": [], "total": 0, "corpusSize": 0, "sources": []}

    articles = client.get("/articles").json()
    assert articles["items"] == [] and articles["total"] == 0
    assert articles["sources"] == [] and articles["days"] == []


def test_health_overview_says_not_run_rather_than_healthy(empty_db_client) -> None:
    """A pipeline that has never run is not a healthy pipeline and is not
    a failing one — it has no state to report. Every stage must say so
    explicitly instead of defaulting to a status that reads as a result.
    """
    client, _ = empty_db_client
    body = client.get("/health/overview").json()
    assert body["last_run_at"] is None
    assert body["healthy"] == 0 and body["needs_attention"] == 0
    for stage in body["stages"]:
        assert stage["status"] == "not_run", stage
        assert stage["ran_at"] is None
        assert stage["detail"] == "", f"a stage that never ran must not describe what it did: {stage}"
