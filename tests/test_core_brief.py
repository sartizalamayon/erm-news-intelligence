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
    conn.commit()

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
