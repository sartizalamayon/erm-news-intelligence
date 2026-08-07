"""Daily Risk Insight brief drafting (frontend-contract.md §3.5): one
bullet per approved article, grouped by the 5 fixed risk sections, with
an editable "fuel price" line carried under the Macroeconomic section.

Calls the LLM through `app.services.llm.registry.build_client_for_feature`
(the `SUMMARIZATION` route, app.services.llm.routing) ->
`LLMClient.draft_bullet(title, body, section)` — the frozen P3 prompt
(app/services/llm/prompts.py's `draft_bullet_system`/`draft_bullet_prompt`,
byte-verified against legacy in tests/test_p3_prompts.py). This module
never reimplements provider or prompt logic, only orchestrates which
approved articles feed the call and shapes the response — exactly the
same "faithful port, never reimplemented" boundary app.services.enrichment
draws around every other P3 stage.

`draft_bullet`'s frozen prompt asks for exactly one " – " (space,
en-dash, space) separator between a short bold hook and the rest of the
sentence (see draft_bullet_system's own instruction) — `_split_bullet`
below relies on that literal separator to build the `{lead, rest}` shape
frontend-contract.md §3.5 documents; this is reading the existing
contract, not inventing a new one.

Draft persistence and the fuel line live in the generic `settings`
key/value table (0001_settings.sql), same reuse-not-reinvent move as
app/services/export.py's presets and app/api/routers/review.py's
star/note (see those modules' docstrings for the fuller rationale and
the flag back to ARCH-DB about dedicated tables).
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any

from app.db.connection import DictConnection
from app.db.settings import get_setting, set_setting
from app.errors import ConfigurationError

RISK_SECTIONS: tuple[str, ...] = ("Geopolitical", "Macroeconomy", "Telco/regulatory", "Political", "Others")
SECTION_TITLES: dict[str, str] = {
    "Geopolitical": "◆ Geopolitical Outlook",
    "Macroeconomy": "◆ Macroeconomic Outlook",
    "Telco/regulatory": "◆ Telecom & Regulatory",
    "Political": "◆ Political Outlook",
    "Others": "◆ Other Developments",
}
FUEL_LINE_SECTION = "Macroeconomy"
FUEL_SETTINGS_KEY = "brief_fuel_line"
BULLETS_PER_SECTION = 3
BULLET_SEPARATOR = " – "  # space, en-dash, space — draft_bullet_system's mandated format.


def get_fuel_line(conn: DictConnection) -> str:
    return get_setting(conn, FUEL_SETTINGS_KEY, default="")


def set_fuel_line(conn: DictConnection, fuel: str) -> None:
    set_setting(conn, FUEL_SETTINGS_KEY, fuel or "")


def _draft_key(day: date) -> str:
    return f"brief_draft::{day.isoformat()}"


def load_draft(conn: DictConnection, day: date) -> dict[str, Any] | None:
    raw = get_setting(conn, _draft_key(day), default="")
    if not raw.strip():
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def _approved_articles_for_section(conn: DictConnection, day: date, section: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT a.id, a.title, a.title_en, COALESCE(ab.content_text, '') AS content_text
        FROM articles a LEFT JOIN article_bodies ab ON ab.article_id = a.id
        WHERE a.day = %(day)s AND a.review_status = 'approved'
          AND COALESCE(a.is_primary, TRUE) = TRUE
          AND EXISTS (SELECT 1 FROM unnest(string_to_array(a.sections, ', ')) s WHERE s = %(section)s)
        ORDER BY a.llm_score DESC NULLS LAST, a.id
        LIMIT %(limit)s
        """,
        {"day": day, "section": section, "limit": BULLETS_PER_SECTION},
    ).fetchall()
    return [dict(r) for r in rows]


def _split_bullet(text: str) -> dict[str, str]:
    if BULLET_SEPARATOR in text:
        lead, rest = text.split(BULLET_SEPARATOR, 1)
        return {"lead": lead.strip(), "rest": rest.strip()}
    return {"lead": text.strip(), "rest": ""}


def _build_client(conn: DictConnection) -> Any:
    """Lazy import, mirroring app.services.admin's own reasoning for the
    same pattern: app.services.llm.registry pulls in app.services.llm.transport,
    P3-LLM's concurrently-owned territory — importing it at call time
    rather than module load time means this module stays importable even
    mid-refactor over there.
    """
    from app.services.llm.registry import build_client_for_feature
    from app.services.llm.routing import SUMMARIZATION

    try:
        return build_client_for_feature(conn, SUMMARIZATION)
    except ConfigurationError:
        return None


def generate_draft(conn: DictConnection, *, day: date, fuel: str | None = None) -> dict[str, Any]:
    if fuel is not None:
        set_fuel_line(conn, fuel)
    fuel_value = get_fuel_line(conn)

    client = _build_client(conn)

    sections_out = []
    for section in RISK_SECTIONS:
        articles = _approved_articles_for_section(conn, day, section)
        bullets = []
        for art in articles:
            title = art["title_en"] or art["title"]
            if client is not None:
                text = client.draft_bullet(title, art["content_text"], section)
            else:
                # No LLM provider configured for this feature (routine
                # state — see app.services.llm.registry) -> fall back to
                # the bare headline as the bullet, same "degrade, never
                # crash" posture every other P3 stage uses.
                text = title
            bullets.append(_split_bullet(text))
        sections_out.append(
            {
                "title": SECTION_TITLES.get(section, section),
                "fuelLine": fuel_value if section == FUEL_LINE_SECTION else "",
                "bullets": bullets,
                "empty": not bullets,
            }
        )

    draft = {"fuel": fuel_value, "ready": True, "sections": sections_out}
    set_setting(conn, _draft_key(day), json.dumps(draft))
    return draft
