"""Tests for the Daily Risk Insight brief (frontend-contract.md §3.5):
GET/POST /brief/draft, GET /brief/download.docx, PUT /config/fuel-line.

The LLM is always mocked here (mission §6.4 cost discipline: unit tests
never spend real tokens) — `app.services.brief._build_client` is
monkeypatched to return a real `LLMClient` wired to
`tests/helpers/fake_llm.py`'s `FakeLLMProvider` transport, so the actual
frozen prompt-building/parsing path (app.services.llm.prompts/parsing)
still runs for real, only the network call is faked. This also proves
`_split_bullet`'s reliance on the " – " separator against the real
`LLMClient.draft_bullet` -> `parsing.parse_draft_bullet` pipeline, not a
hand-rolled stand-in.
"""

from __future__ import annotations

import contextlib
import itertools
from datetime import date

import pytest
from fastapi.testclient import TestClient

from app.api.deps import AuthenticatedUser, get_current_user
from app.api.schemas.core import _reset_risk_sections_cache_for_tests
from app.db.testing import migrated_schema, pg_available
from app.main import create_app
from app.services.llm.provider import LLMClient
from tests.helpers.fake_llm import FakeLLMProvider

pytestmark = pytest.mark.skipif(not pg_available(), reason="no reachable Postgres (set TEST_DATABASE_URL)")

USER = AuthenticatedUser(id=1, email="analyst@grameenphone.com", role="analyst", display_name="Analyst", session_id=1)

_url_seq = itertools.count()


def _insert_article(conn, **fields):
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


def _fake_client(bullet_text: str = "Fake hook – fake rest of the sentence.") -> LLMClient:
    fake = FakeLLMProvider(responses=lambda prompt, system: bullet_text)
    return LLMClient(transport=fake, name="fake")


@pytest.fixture
def brief_client(monkeypatch: pytest.MonkeyPatch):
    import app.api.routers.brief as brief_router_module

    with migrated_schema() as conn:
        monkeypatch.setattr(brief_router_module, "get_connection", lambda: contextlib.nullcontext(conn))
        app = create_app()
        app.dependency_overrides[get_current_user] = lambda: USER
        with TestClient(app) as client:
            yield client, conn


def test_get_draft_unauthenticated_returns_401() -> None:
    app = create_app()
    with TestClient(app) as client:
        resp = client.get("/brief/draft")
    assert resp.status_code == 401


def test_post_draft_unauthenticated_returns_401() -> None:
    app = create_app()
    with TestClient(app) as client:
        resp = client.post("/brief/draft", json={"fuel": "x"})
    assert resp.status_code == 401


def test_fuel_line_unauthenticated_returns_401() -> None:
    app = create_app()
    with TestClient(app) as client:
        resp = client.put("/config/fuel-line", json={"fuel": "x"})
    assert resp.status_code == 401


def test_get_draft_with_no_prior_draft_returns_404(brief_client) -> None:
    client, _conn = brief_client
    resp = client.get("/brief/draft", params={"date": date.today().isoformat()})
    assert resp.status_code == 404


def test_put_fuel_line_persists(brief_client) -> None:
    client, conn = brief_client
    resp = client.put("/config/fuel-line", json={"fuel": "Fuel Prices: Crude - $70.2"})
    assert resp.status_code == 200
    from app.services.brief import get_fuel_line

    assert get_fuel_line(conn) == "Fuel Prices: Crude - $70.2"


def test_post_draft_generates_bullets_from_approved_articles_only(
    brief_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, conn = brief_client
    import app.services.brief as brief_service_module

    bullet_text = "Port policy tension – Chattogram committee calls to halt leasing."
    monkeypatch.setattr(brief_service_module, "_build_client", lambda conn: _fake_client(bullet_text))

    today = date.today()
    _insert_article(
        conn, title="Approved macro story", day=today,
        sections="Macroeconomy", review_status="approved", llm_score=9,
    )
    _insert_article(
        conn, title="Pending story, not approved", day=today,
        sections="Macroeconomy", review_status="pending", llm_score=9,
    )
    # Configure "Geopolitical" explicitly. The brief used to iterate a
    # frozen five-name tuple, so this section appeared whether or not it
    # existed; it now iterates the sections that are CONFIGURED or that
    # the day's approved articles actually carry. The assertion below --
    # a section with no approved stories reports empty rather than being
    # absent -- is only meaningful for a section that is really there,
    # which is what this row makes true.
    conn.execute(
        "INSERT INTO keywords (term_en, term_bn, section, enabled) VALUES (%s, %s, %s, TRUE)",
        ("sanctions", "", "Geopolitical"),
    )
    conn.commit()
    _reset_risk_sections_cache_for_tests()

    resp = client.post(
        "/brief/draft", json={"fuel": "Fuel Prices: Crude – $70.2"}, params={"date": today.isoformat()}
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["ready"] is True
    assert data["fuel"] == "Fuel Prices: Crude – $70.2"

    macro = next(s for s in data["sections"] if "Macroeconomic" in s["title"])
    assert macro["empty"] is False
    assert len(macro["bullets"]) == 1
    assert macro["bullets"][0]["lead"] == "Port policy tension"
    assert macro["bullets"][0]["rest"].startswith("Chattogram committee")
    assert macro["fuelLine"] == "Fuel Prices: Crude – $70.2"

    geo = next(s for s in data["sections"] if "Geopolitical" in s["title"])
    assert geo["empty"] is True
    assert geo["bullets"] == []
    assert geo["fuelLine"] == ""


def test_get_draft_after_post_returns_the_persisted_draft(brief_client, monkeypatch: pytest.MonkeyPatch) -> None:
    client, conn = brief_client
    import app.services.brief as brief_service_module

    monkeypatch.setattr(brief_service_module, "_build_client", lambda conn: _fake_client("Hook – rest."))

    today = date.today()
    _insert_article(
        conn, title="Approved story", day=today, sections="Political", review_status="approved", llm_score=8
    )
    conn.commit()

    posted = client.post("/brief/draft", json={}, params={"date": today.isoformat()})
    assert posted.status_code == 200

    got = client.get("/brief/draft", params={"date": today.isoformat()})
    assert got.status_code == 200
    assert got.json()["sections"] == posted.json()["sections"]


def test_download_docx_with_no_draft_returns_404(brief_client) -> None:
    client, _conn = brief_client
    resp = client.get("/brief/download.docx", params={"date": date.today().isoformat()})
    assert resp.status_code == 404


def test_download_docx_returns_a_real_document_matching_the_draft(
    brief_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, conn = brief_client
    import app.services.brief as brief_service_module

    monkeypatch.setattr(brief_service_module, "_build_client", lambda conn: _fake_client("Hook – rest of it."))

    today = date.today()
    _insert_article(
        conn, title="Approved macro story", day=today, sections="Macroeconomy", review_status="approved", llm_score=9
    )
    conn.commit()
    client.post("/brief/draft", json={}, params={"date": today.isoformat()})

    resp = client.get("/brief/download.docx", params={"date": today.isoformat()})
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    assert "attachment" in resp.headers["content-disposition"]

    import io

    from docx import Document

    doc = Document(io.BytesIO(resp.content))
    text = "\n".join(p.text for p in doc.paragraphs)
    assert "Daily Risk Insights" in text
    assert "Hook" in text
    assert "rest of it." in text


def test_an_admin_created_section_reaches_the_brief(brief_client) -> None:
    """The brief iterated a frozen five-name tuple, so an approved
    article in a section an admin had created never reached the Word
    document, and nothing surfaced it: the section simply was not there.
    Sections are admin-creatable (any non-empty string in
    `keywords.section`, and the .xlsx import can create them too), so
    this dropped exactly the coverage somebody went out of their way to
    start tracking.
    """
    client, conn = brief_client
    today = date.today()
    conn.execute(
        "INSERT INTO keywords (term_en, term_bn, section, enabled) VALUES (%s, %s, %s, TRUE)",
        ("submarine cable", "", "Infrastructure"),
    )
    _insert_article(
        conn, title="Cable cut disrupts bandwidth", day=today,
        sections="Infrastructure", review_status="approved", llm_score=8,
    )
    conn.commit()
    _reset_risk_sections_cache_for_tests()

    data = client.post("/brief/draft", json={}, params={"date": today.isoformat()}).json()
    titles = [s["title"] for s in data["sections"]]
    assert "Infrastructure" in titles, f"admin-created section missing from the brief: {titles}"
    infra = next(s for s in data["sections"] if s["title"] == "Infrastructure")
    assert infra["empty"] is False
    assert len(infra["bullets"]) == 1


def test_an_approved_article_survives_its_section_being_deleted(brief_client) -> None:
    """`articles.sections` is written at COLLECTION time, so an approved
    article can carry a section whose keyword row was renamed or deleted
    afterwards. Iterating only the configured set would drop it silently.
    A story somebody approved for the brief belongs in the brief.
    """
    client, conn = brief_client
    today = date.today()
    _insert_article(
        conn, title="Orphaned but approved", day=today,
        sections="Retired Section", review_status="approved", llm_score=7,
    )
    conn.commit()
    _reset_risk_sections_cache_for_tests()

    data = client.post("/brief/draft", json={}, params={"date": today.isoformat()}).json()
    titles = [s["title"] for s in data["sections"]]
    assert "Retired Section" in titles, f"approved article dropped with its section: {titles}"


def test_an_undrafted_bullet_is_marked_as_the_raw_headline(brief_client) -> None:
    """With no SUMMARIZATION provider resolving, `draft_bullet` is never
    called and the bullet is the article's own headline. Degrading that
    way is right; what was wrong is that the result was
    indistinguishable from drafted prose once it reached the .docx, so a
    document that went to a board could be entirely undrafted headlines
    and read as finished work.
    """
    client, conn = brief_client
    today = date.today()
    conn.execute(
        "INSERT INTO keywords (term_en, term_bn, section, enabled) VALUES (%s, %s, %s, TRUE)",
        ("inflation", "", "Macroeconomy"),
    )
    _insert_article(
        conn, title="Undrafted headline stands alone", day=today,
        sections="Macroeconomy", review_status="approved", llm_score=9,
    )
    conn.commit()
    _reset_risk_sections_cache_for_tests()

    data = client.post("/brief/draft", json={}, params={"date": today.isoformat()}).json()
    macro = next(s for s in data["sections"] if "Macroeconomic" in s["title"])
    assert macro["bullets"], "expected the approved article to produce a bullet"
    assert macro["bullets"][0]["drafted"] is False

    resp = client.get("/brief/download.docx", params={"date": today.isoformat()})
    assert resp.status_code == 200

    import io

    from docx import Document

    doc = Document(io.BytesIO(resp.content))
    text = "\n".join(p.text for p in doc.paragraphs)
    assert "not drafted" in text, "an undrafted bullet must be marked in the document itself"
