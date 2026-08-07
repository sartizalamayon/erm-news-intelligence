"""Business logic for the admin console API (ADMIN-API mission).

Every function here takes an already-open `DictConnection` (the same
convention as `app.services.storage`/`app.services.auth`) and returns
plain, JSON-safe dicts (or dataclasses from a package this task consumes
rather than reimplements) — never a raw DB row or a datetime object — so
`app/api/routers/*.py` can hand results straight to a pydantic response
model. Route-layer admin enforcement (`require_admin`) is NOT this
module's job; every function here is reachable to any caller that holds
a connection, exactly like `app.services.storage`'s functions are. The
gate is `app.api.deps.require_admin`, applied per-route in the routers.

Sections below mirror the routers that call them:
  * Sources   -> app.api.routers.sources / .health (test, diagnose)
  * Keywords  -> app.api.routers.keywords
  * Prompts, provider routing/quota -> app.api.routers.prompts
  * Storage, export/import -> app.api.routers.dashboard
  * Source health, pipeline stage runs -> app.api.routers.health

Schema note: this task may not touch migrations/**. Two admin surfaces
have no dedicated table (prompt version history, per-stage-per-day
pipeline status) — both are stored as JSON in the generic `settings`
key/value table (0001_settings.sql), the exact pattern
`app.services.llm.routing`/`app.services.llm.quota` already established
for the identical constraint ("no dedicated quota table exists yet...
persisted through the existing generic settings key/value table").
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any

from psycopg.errors import UniqueViolation
from psycopg.types.json import Jsonb

from app.db.connection import DictConnection
from app.db.settings import get_setting, set_setting
from app.errors import SourceError, ValidationError
from app.services.data_transfer import ExportManifest, ImportReport, export_to_jsonl, import_from_jsonl
from app.services.llm import quota as llm_quota
from app.services.llm import routing as llm_routing
from app.services.llm.prompts import (
    RELEVANCE_EXAMPLES_KEY,
    RELEVANCE_GUIDANCE_KEY,
    get_relevance_examples,
    get_relevance_guidance,
)
from app.services.matching import is_bengali, load_patterns, match_article
from app.services.storage import (
    PurgeResult,
    StorageUsage,
    get_storage_usage,
    purge_matched_articles,
    purge_stale_bodies,
    purge_stale_unmatched_articles,
)

_CHECK = "✓"
_CROSS = "✕"

# ═════════════════════════════════════════════════════════════════════
# Sources (frontend-contract.md §3.7 / §3.10)
# ═════════════════════════════════════════════════════════════════════

# `sources.method` is a single TEXT column (migrations/0002); the
# frontend contract's `s.methods[]` is an array (§3.7 notes this as an
# unresolved open question, "cannot be resolved from static reading").
# Storing multiple methods as a comma-joined string in that one column
# is the pragmatic choice available without a schema change — this task
# may not touch migrations/**.


def _methods_to_str(methods: list[str]) -> str:
    return ",".join(m.strip() for m in methods if m.strip())


def _str_to_methods(method: str) -> list[str]:
    return [m.strip() for m in (method or "").split(",") if m.strip()]


def _source_row_to_dict(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "methods": _str_to_methods(row["method"]),
        "language": row["language"],
        "enabled": row["enabled"],
        "config": dict(row["config"]) if row.get("config") else {},
        "created_at": row["created_at"].isoformat() if row.get("created_at") else None,
    }


def list_sources(conn: DictConnection, *, enabled: bool | None = None) -> list[dict[str, Any]]:
    if enabled is None:
        rows = conn.execute("SELECT * FROM sources ORDER BY name").fetchall()
    else:
        rows = conn.execute("SELECT * FROM sources WHERE enabled = %s ORDER BY name", (enabled,)).fetchall()
    return [_source_row_to_dict(dict(r)) for r in rows]


def get_source(conn: DictConnection, source_id: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM sources WHERE id = %s", (source_id,)).fetchone()
    return _source_row_to_dict(dict(row)) if row else None


def create_source(
    conn: DictConnection,
    *,
    name: str,
    methods: list[str],
    language: str = "mixed",
    enabled: bool = True,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not name.strip():
        raise ValidationError("source name is required")
    if not methods:
        raise ValidationError("at least one fetch method is required")
    try:
        row = conn.execute(
            "INSERT INTO sources (name, method, config, language, enabled) VALUES (%s, %s, %s, %s, %s) RETURNING *",
            (name.strip(), _methods_to_str(methods), Jsonb(config or {}), language, enabled),
        ).fetchone()
    except UniqueViolation as exc:
        conn.rollback()
        raise ValidationError(f"a source named {name!r} already exists") from exc
    conn.commit()
    assert row is not None
    return _source_row_to_dict(dict(row))


def update_source(conn: DictConnection, source_id: int, **fields: Any) -> dict[str, Any] | None:
    existing = conn.execute("SELECT id FROM sources WHERE id = %s", (source_id,)).fetchone()
    if existing is None:
        return None

    sets: list[str] = []
    params: dict[str, Any] = {"id": source_id}
    if fields.get("name") is not None:
        sets.append("name = %(name)s")
        params["name"] = fields["name"]
    if fields.get("methods") is not None:
        if not fields["methods"]:
            raise ValidationError("at least one fetch method is required")
        sets.append("method = %(method)s")
        params["method"] = _methods_to_str(fields["methods"])
    if fields.get("language") is not None:
        sets.append("language = %(language)s")
        params["language"] = fields["language"]
    if fields.get("enabled") is not None:
        sets.append("enabled = %(enabled)s")
        params["enabled"] = fields["enabled"]
    if fields.get("config") is not None:
        sets.append("config = %(config)s")
        params["config"] = Jsonb(fields["config"])

    if not sets:
        return get_source(conn, source_id)

    try:
        row = conn.execute(f"UPDATE sources SET {', '.join(sets)} WHERE id = %(id)s RETURNING *", params).fetchone()
    except UniqueViolation as exc:
        conn.rollback()
        raise ValidationError(f"a source named {fields.get('name')!r} already exists") from exc
    conn.commit()
    assert row is not None
    return _source_row_to_dict(dict(row))


def delete_source(conn: DictConnection, source_id: int) -> bool:
    row = conn.execute("DELETE FROM sources WHERE id = %s RETURNING id", (source_id,)).fetchone()
    conn.commit()
    return row is not None


def toggle_source(conn: DictConnection, source_id: int) -> dict[str, Any] | None:
    row = conn.execute("UPDATE sources SET enabled = NOT enabled WHERE id = %s RETURNING *", (source_id,)).fetchone()
    if row is None:
        return None
    conn.commit()
    return _source_row_to_dict(dict(row))


# ── Live probes: test / diagnose, against the app.sources plugin layer ──
# Read-only use of app.sources.registry (P1-INGEST's territory, not
# edited here) — the registry maps a plugin `key` to a `NewsSource`
# class; this maps the admin `sources` table's `name` column to that
# same class via its `display_name`, the convention every concrete
# source module already follows (see e.g. app/sources/prothom_alo.py).

_registry_loaded = False


def _ensure_registry_loaded() -> None:
    global _registry_loaded
    if not _registry_loaded:
        from app.sources.registry import load_all

        load_all()
        _registry_loaded = True


def _plugin_for_source_name(name: str) -> Any | None:
    from app.sources.registry import all_sources

    _ensure_registry_loaded()
    for source_cls in all_sources().values():
        if getattr(source_cls, "display_name", source_cls.key) == name:
            return source_cls
    return None


def _today_dhaka() -> date:
    # Dhaka is a fixed UTC+6 offset, no DST (matches
    # migrations/0003_articles.sql's `published` column comment).
    return (datetime.now(timezone.utc) + timedelta(hours=6)).date()


def probe_source(conn: DictConnection, source_id: int) -> dict[str, Any] | None:
    """`s.test` / `h.test` (frontend-contract.md §3.7/§3.10): a live
    "test this source now" call against the registered fetch plugin,
    returning a real result string, not a cached status read.
    """
    row = get_source(conn, source_id)
    if row is None:
        return None
    method_label = row["methods"][0] if row["methods"] else ""
    source_cls = _plugin_for_source_name(row["name"])
    if source_cls is None:
        return {
            "source_id": source_id,
            "ok": False,
            "fetched": 0,
            "today": 0,
            "method": method_label,
            "detail": f"no fetch plugin registered for {row['name']!r} yet (app.sources.registry)",
        }
    try:
        items = source_cls().iter_feed_items()
    except SourceError as exc:
        return {
            "source_id": source_id,
            "ok": False,
            "fetched": 0,
            "today": 0,
            "method": method_label,
            "detail": f"fetch failed: {exc}",
        }
    today = _today_dhaka()
    dhaka = timezone(timedelta(hours=6))
    today_count = sum(1 for it in items if it.published_at and it.published_at.astimezone(dhaka).date() == today)
    method_label = method_label or getattr(source_cls, "key", "")
    return {
        "source_id": source_id,
        "ok": True,
        "fetched": len(items),
        "today": today_count,
        "method": method_label,
        "detail": f"{len(items)} fetched, {today_count} from today (via {method_label})",
    }


def diagnose_source(conn: DictConnection, source_id: int) -> dict[str, Any] | None:
    """`h.diagnose` (frontend-contract.md §3.10) — genuine per-check
    diagnostics (reachable / feed discovery / one article body fetch /
    publish-date presence), never the mock's fabricated fixed string.
    """
    row = get_source(conn, source_id)
    if row is None:
        return None
    source_cls = _plugin_for_source_name(row["name"])
    if source_cls is None:
        return {
            "source_id": source_id,
            "reachable": False,
            "feed_discovery_ok": False,
            "article_url_found": False,
            "date_parse_ok": False,
            "detail": f"no fetch plugin registered for {row['name']!r} yet (app.sources.registry)",
        }

    instance = source_cls()
    try:
        items = instance.iter_feed_items()
    except SourceError as exc:
        return {
            "source_id": source_id,
            "reachable": False,
            "feed_discovery_ok": False,
            "article_url_found": False,
            "date_parse_ok": False,
            "detail": f"reachable {_CROSS} · {exc}",
        }

    feed_ok = len(items) > 0
    article_ok = False
    article_detail = ""
    if items:
        try:
            article = instance.fetch_article(items[0])
            article_ok = bool(article.body_text.strip())
        except SourceError as exc:
            article_detail = f" ({exc})"
    date_ok = any(it.published_at is not None for it in items)

    detail = " · ".join(
        [
            f"reachable {_CHECK}",
            f"feed discovery {_CHECK if feed_ok else _CROSS} ({len(items)} items)",
            f"article body fetch {_CHECK if article_ok else _CROSS}{article_detail}",
            f"date parse {_CHECK if date_ok else _CROSS + ' (no machine-readable publish date found)'}",
        ]
    )
    return {
        "source_id": source_id,
        "reachable": True,
        "feed_discovery_ok": feed_ok,
        "article_url_found": article_ok,
        "date_parse_ok": date_ok,
        "detail": detail,
    }


# ═════════════════════════════════════════════════════════════════════
# Keywords (frontend-contract.md §3.8)
# ═════════════════════════════════════════════════════════════════════


def _keyword_row_to_dict(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "section": row["section"],
        "term_en": row["term_en"],
        "term_bn": row["term_bn"],
        "enabled": row["enabled"],
        "created_at": row["created_at"].isoformat() if row.get("created_at") else None,
    }


def list_keywords(conn: DictConnection, *, section: str | None = None) -> list[dict[str, Any]]:
    if section:
        rows = conn.execute("SELECT * FROM keywords WHERE section = %s ORDER BY term_en", (section,)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM keywords ORDER BY section, term_en").fetchall()
    return [_keyword_row_to_dict(dict(r)) for r in rows]


def list_keyword_sections(conn: DictConnection) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT section, count(*) AS count FROM keywords GROUP BY section ORDER BY section").fetchall()
    return [{"section": r["section"], "count": r["count"]} for r in rows]


def get_keyword(conn: DictConnection, keyword_id: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM keywords WHERE id = %s", (keyword_id,)).fetchone()
    return _keyword_row_to_dict(dict(row)) if row else None


def create_keyword(
    conn: DictConnection, *, section: str, term_en: str = "", term_bn: str = "", enabled: bool = True
) -> dict[str, Any]:
    if not section.strip():
        raise ValidationError("section is required")
    if not term_en.strip() and not term_bn.strip():
        raise ValidationError("at least one of term_en/term_bn is required")
    row = conn.execute(
        "INSERT INTO keywords (section, term_en, term_bn, enabled) VALUES (%s, %s, %s, %s) RETURNING *",
        (section.strip(), term_en.strip(), term_bn.strip(), enabled),
    ).fetchone()
    conn.commit()
    assert row is not None
    return _keyword_row_to_dict(dict(row))


def update_keyword(conn: DictConnection, keyword_id: int, **fields: Any) -> dict[str, Any] | None:
    existing = conn.execute("SELECT id FROM keywords WHERE id = %s", (keyword_id,)).fetchone()
    if existing is None:
        return None
    sets: list[str] = []
    params: dict[str, Any] = {"id": keyword_id}
    for col in ("section", "term_en", "term_bn", "enabled"):
        if fields.get(col) is not None:
            sets.append(f"{col} = %({col})s")
            params[col] = fields[col]
    if not sets:
        return get_keyword(conn, keyword_id)
    row = conn.execute(f"UPDATE keywords SET {', '.join(sets)} WHERE id = %(id)s RETURNING *", params).fetchone()
    conn.commit()
    assert row is not None
    return _keyword_row_to_dict(dict(row))


def delete_keyword(conn: DictConnection, keyword_id: int) -> bool:
    row = conn.execute("DELETE FROM keywords WHERE id = %s RETURNING id", (keyword_id,)).fetchone()
    conn.commit()
    return row is not None


def toggle_keyword(conn: DictConnection, keyword_id: int) -> dict[str, Any] | None:
    row = conn.execute("UPDATE keywords SET enabled = NOT enabled WHERE id = %s RETURNING *", (keyword_id,)).fetchone()
    if row is None:
        return None
    conn.commit()
    return _keyword_row_to_dict(dict(row))


def rematch_today(conn: DictConnection) -> dict[str, int]:
    """`rematch` ("Rematch today", §3.8) — re-runs P2's keyword filter
    (app.services.matching) against today's already-fetched articles,
    using whatever the `keywords` table looks like RIGHT NOW (so an
    admin's just-added/edited/disabled keyword takes effect immediately
    on the next rematch, without waiting for tomorrow's collection run).

    Scoped to `review_status IN ('pending', 'filtered_out')` only —
    deliberately never touches 'approved'/'rejected' rows, so a rematch
    can never silently undo an analyst's already-made review decision.
    """
    patterns = load_patterns(conn)
    rows = conn.execute(
        "SELECT id, title FROM articles WHERE day = CURRENT_DATE AND review_status IN ('pending', 'filtered_out')"
    ).fetchall()
    matched = filtered_out = 0
    for row in rows:
        result = match_article({"title": row["title"]}, patterns)
        conn.execute(
            "UPDATE articles SET sections = %s, keywords = %s, review_status = %s, match_type = %s WHERE id = %s",
            (result["sections"], result["keywords"], result["review_status"], result["match_type"], row["id"]),
        )
        if result["review_status"] == "pending":
            matched += 1
        else:
            filtered_out += 1
    conn.commit()
    return {"matched": matched, "filtered_out": filtered_out, "total": len(rows)}


# ═════════════════════════════════════════════════════════════════════
# AI prompts + versioning (frontend-contract.md §3.9)
# ═════════════════════════════════════════════════════════════════════
#
# No dedicated table (this task may not touch migrations/**) — version
# history is JSON in the `settings` KV table, keyed
# "prompt_versions:{field}", the identical pattern
# app.services.llm.routing/quota already use for the same constraint.
# The ACTIVE version's content is ALSO mirrored into the plain
# `relevance_guidance`/`relevance_examples` settings keys
# (app.services.llm.prompts' RELEVANCE_GUIDANCE_KEY/RELEVANCE_EXAMPLES_KEY)
# on every save/activate, since those are the keys P3's actual runtime
# read path (get_relevance_guidance/get_relevance_examples) consults —
# versioning must never require a change to that frozen read path.

PROMPT_FIELDS: dict[str, str] = {
    "guidance": RELEVANCE_GUIDANCE_KEY,
    "examples": RELEVANCE_EXAMPLES_KEY,
}


def _validate_prompt_field(field: str) -> None:
    if field not in PROMPT_FIELDS:
        raise ValidationError(f"unknown prompt field {field!r} — must be one of {sorted(PROMPT_FIELDS)}")


def _versions_settings_key(field: str) -> str:
    return f"prompt_versions:{field}"


def _load_prompt_versions(conn: DictConnection, field: str) -> dict[str, Any]:
    raw = get_setting(conn, _versions_settings_key(field), default="")
    if raw.strip():
        try:
            data = json.loads(raw)
            if isinstance(data, dict) and data.get("versions"):
                return data
        except json.JSONDecodeError:
            pass
    # No history yet — seed version 1 from whatever P3's runtime read
    # path would currently return (get_relevance_guidance/_examples's own
    # fallback semantics), so there is always at least one real,
    # retrievable version rather than an empty list.
    current = get_relevance_guidance(conn) if field == "guidance" else get_relevance_examples(conn)
    seed = {
        "active": 1,
        "versions": [
            {
                "version": 1,
                "content": current,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "created_by": "system",
                "note": "initial version, seeded from the existing configured value",
            }
        ],
    }
    set_setting(conn, _versions_settings_key(field), json.dumps(seed))
    return seed


def _save_prompt_versions(conn: DictConnection, field: str, data: dict[str, Any]) -> None:
    set_setting(conn, _versions_settings_key(field), json.dumps(data))


def list_prompt_versions(conn: DictConnection, field: str) -> list[dict[str, Any]]:
    _validate_prompt_field(field)
    data = _load_prompt_versions(conn, field)
    active = data["active"]
    return [{**v, "active": v["version"] == active} for v in data["versions"]]


def save_prompt_content(
    conn: DictConnection, field: str, content: str, *, actor_email: str, note: str = ""
) -> dict[str, Any]:
    """Every save creates a NEW version and makes it active immediately
    (DoD #3: "every change creates a new version"). Never mutates an
    existing version's content — history is append-only.
    """
    _validate_prompt_field(field)
    data = _load_prompt_versions(conn, field)
    next_version = max((v["version"] for v in data["versions"]), default=0) + 1
    entry = {
        "version": next_version,
        "content": content,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "created_by": actor_email,
        "note": note,
    }
    data["versions"].append(entry)
    data["active"] = next_version
    _save_prompt_versions(conn, field, data)
    set_setting(conn, PROMPT_FIELDS[field], content)
    conn.commit()
    return {**entry, "active": True}


def activate_prompt_version(conn: DictConnection, field: str, version: int) -> dict[str, Any] | None:
    """Repoints "active" at an EXISTING version (DoD #3: "the active
    version is selectable") — does not create a new version entry, since
    the content itself hasn't changed, only which one is live.
    """
    _validate_prompt_field(field)
    data = _load_prompt_versions(conn, field)
    match = next((v for v in data["versions"] if v["version"] == version), None)
    if match is None:
        return None
    data["active"] = version
    _save_prompt_versions(conn, field, data)
    set_setting(conn, PROMPT_FIELDS[field], match["content"])
    conn.commit()
    return {**match, "active": True}


def get_prompt_config(conn: DictConnection) -> dict[str, Any]:
    guidance_data = _load_prompt_versions(conn, "guidance")
    examples_data = _load_prompt_versions(conn, "examples")
    guidance_active = next(v for v in guidance_data["versions"] if v["version"] == guidance_data["active"])
    examples_active = next(v for v in examples_data["versions"] if v["version"] == examples_data["active"])
    approved_row = conn.execute("SELECT count(*) AS n FROM articles WHERE review_status = 'approved'").fetchone()
    rejected_row = conn.execute("SELECT count(*) AS n FROM articles WHERE review_status = 'rejected'").fetchone()
    return {
        "guidance": guidance_active["content"],
        "examples": examples_active["content"],
        "guidance_version": guidance_data["active"],
        "examples_version": examples_data["active"],
        "approved_count": approved_row["n"] if approved_row else 0,
        "rejected_count": rejected_row["n"] if rejected_row else 0,
    }


# ── Keyword-candidate mining (§3.9 candidates[]) ────────────────────────
# A lean, dependency-free word-frequency co-occurrence miner over real
# approve/reject decision history — no new dependency (GATE 0 / mission
# "LEAN"), replacing the mock's static illustrative sample.

_STOPWORDS = {
    "the",
    "a",
    "an",
    "to",
    "of",
    "in",
    "on",
    "for",
    "and",
    "or",
    "is",
    "are",
    "was",
    "were",
    "by",
    "with",
    "as",
    "at",
    "from",
    "after",
    "over",
    "new",
    "says",
    "amid",
    "into",
    "than",
    "will",
    "has",
    "have",
    "had",
    "its",
    "his",
    "her",
    "their",
    "this",
    "that",
    "not",
    "no",
}


def _tokenize(text: str | None) -> set[str]:
    words = re.findall(r"[\w']+", (text or "").lower())
    return {w for w in words if len(w) > 2 and w not in _STOPWORDS}


def mine_keyword_candidates(conn: DictConnection, *, limit: int = 6, sample: int = 300) -> list[dict[str, Any]]:
    approved_rows = conn.execute(
        "SELECT title, title_en FROM articles WHERE review_status = 'approved' ORDER BY id DESC LIMIT %s",
        (sample,),
    ).fetchall()
    rejected_rows = conn.execute(
        "SELECT title, title_en FROM articles WHERE review_status = 'rejected' ORDER BY id DESC LIMIT %s",
        (sample,),
    ).fetchall()
    existing_terms = {
        (row["term_en"] or "").strip().lower() for row in conn.execute("SELECT term_en FROM keywords").fetchall()
    }

    approved_counts: dict[str, int] = {}
    rejected_counts: dict[str, int] = {}
    for row in approved_rows:
        for word in _tokenize(row["title_en"] or row["title"]):
            approved_counts[word] = approved_counts.get(word, 0) + 1
    for row in rejected_rows:
        for word in _tokenize(row["title_en"] or row["title"]):
            rejected_counts[word] = rejected_counts.get(word, 0) + 1

    candidates = [
        {
            "word": word,
            "lang": "BN" if is_bengali(word) else "EN",
            "approved": count,
            "rejected": rejected_counts.get(word, 0),
        }
        for word, count in approved_counts.items()
        if word not in existing_terms
    ]
    candidates.sort(key=lambda c: (-c["approved"], c["rejected"]))
    return candidates[:limit]


def _recent_titles(conn: DictConnection, review_status: str, limit: int = 40) -> list[str]:
    rows = conn.execute(
        "SELECT title_en, title FROM articles WHERE review_status = %s ORDER BY id DESC LIMIT %s",
        (review_status, limit),
    ).fetchall()
    return [(row["title_en"] or row["title"]) for row in rows]


def _build_client_for_feature(conn: DictConnection, feature: str) -> Any:
    """Lazy import of app.services.llm.registry — deliberately NOT a
    module-level import. That module imports app.services.llm.transport's
    concrete transport classes, and both packages are P3-LLM's actively
    concurrent territory; a module-level import here would make this
    entire file (and therefore every admin route, including plain
    keyword/source CRUD that has nothing to do with the LLM layer)
    unimportable during any moment P3-LLM's package is mid-refactor.
    Deferred to call time, the blast radius of that package being
    transiently broken is contained to the one action that genuinely
    needs it (suggest_refinement) rather than the whole admin API.
    """
    from app.services.llm.registry import build_client_for_feature

    return build_client_for_feature(conn, feature)


def suggest_refinement(conn: DictConnection) -> str:
    """ "Suggest a refinement" (§3.9's `suggest`) — a genuine LLM call
    server-side, built on top of P3-LLM's already-provided pieces:
    `LLMClient.suggest_guidance()` (app.services.llm.provider) and its
    routing slot, folded into the `summarization` feature
    (app.services.llm.routing's own docstring names this exact reason).
    Never auto-applied — the caller (PUT /prompts/guidance) is a
    separate, explicit save.
    """
    approved = _recent_titles(conn, "approved")
    rejected = _recent_titles(conn, "rejected")
    client = _build_client_for_feature(conn, llm_routing.SUMMARIZATION)
    return client.suggest_guidance(approved, rejected)


# ═════════════════════════════════════════════════════════════════════
# Provider routing / quota config (ADR §9.1, DoD #4)
# ═════════════════════════════════════════════════════════════════════


def _provider_choice_to_dict(choice: llm_routing.ProviderChoice | None) -> dict[str, Any] | None:
    if choice is None:
        return None
    return {"provider": choice.provider, "model": choice.model}


def _feature_route_to_dict(route: llm_routing.FeatureRoute) -> dict[str, Any]:
    return {"primary": _provider_choice_to_dict(route.primary), "secondary": _provider_choice_to_dict(route.secondary)}


def get_routing_config(conn: DictConnection) -> dict[str, Any]:
    config = llm_routing.load_routing_config(conn)
    return {feature: _feature_route_to_dict(route) for feature, route in config.routes.items()}


def save_routing_config(conn: DictConnection, routes: dict[str, Any]) -> dict[str, Any]:
    """Read-modify-write over the FULL 5-feature map: an admin changing
    just one feature's route (the common case — "route scoring to
    Gemini") should not have to resend the other four untouched. Starts
    from `load_routing_config()`'s current state (already-defaulted for
    any feature the admin has never touched) and overlays only the
    feature(s) present in `routes`, then persists the complete map via
    `app.services.llm.routing.save_routing_config`, which itself always
    writes the full 5-feature map (see that function's own docstring).
    """
    current = llm_routing.load_routing_config(conn)
    built: dict[str, llm_routing.FeatureRoute] = dict(current.routes)
    for feature, route in routes.items():
        primary_raw = route["primary"]
        primary = llm_routing.ProviderChoice(provider=primary_raw["provider"], model=primary_raw.get("model"))
        secondary_raw = route.get("secondary")
        secondary = (
            llm_routing.ProviderChoice(provider=secondary_raw["provider"], model=secondary_raw.get("model"))
            if secondary_raw
            else None
        )
        built[feature] = llm_routing.FeatureRoute(primary=primary, secondary=secondary)
    config = llm_routing.RoutingConfig(routes=built)
    llm_routing.save_routing_config(conn, config)
    conn.commit()
    return {feature: _feature_route_to_dict(route) for feature, route in config.routes.items()}


def _provider_config_row_to_dict(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "provider": row["provider"],
        "model": row["model"],
        "role": row["role"],
        "api_key_last4": row["api_key_last4"],
        "has_key": bool(row["has_key"]),
        "updated_at": row["updated_at"].isoformat() if row.get("updated_at") else None,
    }


# Providers `provider_configs` (ARCH-DB's table) can represent — mirrors
# that table's own CHECK constraint and
# app.services.llm.registry.PROVIDER_CONFIGS_PROVIDERS (not imported
# from there directly: that module transitively imports
# app.services.llm.transport's concrete transport classes and is
# P3-LLM's actively concurrent territory — see
# _build_client_for_feature's docstring for why this file never imports
# it at module level). `llm_routing.KNOWN_PROVIDERS` already holds the
# identical tuple for the identical reason and IS safe to import here
# (app.services.llm.routing has no such transitive dependency).
_PROVIDER_CONFIGS_PROVIDERS = llm_routing.KNOWN_PROVIDERS


def list_provider_configs(conn: DictConnection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT provider, model, role, api_key_last4, updated_at, "
        "(api_key_ciphertext IS NOT NULL) AS has_key FROM provider_configs ORDER BY provider"
    ).fetchall()
    return [_provider_config_row_to_dict(dict(r)) for r in rows]


_PROVIDER_ROLES = ("primary", "secondary", "disabled")


def upsert_provider_config(
    conn: DictConnection,
    provider: str,
    *,
    model: str | None = None,
    role: str | None = None,
    api_key: str | None = None,
    updated_by: int | None = None,
) -> dict[str, Any]:
    """Writes to `provider_configs` (ARCH-DB's table) — restricted to
    `groq`/`gemini` because that table's own `CHECK (provider IN ('groq',
    'gemini'))` constraint makes it structurally impossible to hold
    anything else (a migration this task may not add). This is
    deliberately NOT the generic "admin adds a new provider" surface —
    for that (an arbitrary provider name, its own base_url/adapter, and
    one or more pooled API keys), see `save_provider_definition` /
    `save_provider_credential` below, backed by
    `app.services.llm.provider_registry` (P3-LLM's Task 3 addition).

    Never accepts or returns a plaintext key beyond this one write
    (ADR §9.1) — `api_key`, if given, is encrypted immediately via
    `app.db.crypto` and only the ciphertext + masked suffix are stored;
    the return value never includes it (see ProviderConfigOut).
    """
    if provider not in _PROVIDER_CONFIGS_PROVIDERS:
        raise ValidationError(f"unknown provider {provider!r} — must be one of {_PROVIDER_CONFIGS_PROVIDERS}")
    if role is not None and role not in _PROVIDER_ROLES:
        raise ValidationError(f"role must be one of {_PROVIDER_ROLES}, got {role!r}")

    existing = conn.execute("SELECT model, role FROM provider_configs WHERE provider = %s", (provider,)).fetchone()
    resolved_model = model if model is not None else (existing["model"] if existing else "")
    resolved_role = role if role is not None else (existing["role"] if existing else "secondary")

    ciphertext = None
    last4 = None
    if api_key:
        from app.db.crypto import encrypt_api_key

        ciphertext, last4 = encrypt_api_key(api_key)

    if existing is None:
        conn.execute(
            "INSERT INTO provider_configs (provider, model, role, api_key_ciphertext, api_key_last4, updated_by) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (provider, resolved_model, resolved_role, ciphertext, last4 or "", updated_by),
        )
    elif ciphertext is not None:
        conn.execute(
            "UPDATE provider_configs SET model = %s, role = %s, api_key_ciphertext = %s, api_key_last4 = %s, "
            "updated_by = %s, updated_at = now() WHERE provider = %s",
            (resolved_model, resolved_role, ciphertext, last4, updated_by, provider),
        )
    else:
        conn.execute(
            "UPDATE provider_configs SET model = %s, role = %s, updated_by = %s, updated_at = now() "
            "WHERE provider = %s",
            (resolved_model, resolved_role, updated_by, provider),
        )
    conn.commit()
    row = conn.execute(
        "SELECT provider, model, role, api_key_last4, updated_at, "
        "(api_key_ciphertext IS NOT NULL) AS has_key FROM provider_configs WHERE provider = %s",
        (provider,),
    ).fetchone()
    assert row is not None
    return _provider_config_row_to_dict(dict(row))


def _quota_status_to_dict(status: llm_quota.QuotaStatus) -> dict[str, Any]:
    return {
        "provider": status.provider,
        "day": status.day.isoformat(),
        "tokens_used": status.used.tokens,
        "requests_used": status.used.requests,
        "tpd": status.limits.tpd,
        "rpd": status.limits.rpd,
        "tokens_remaining": status.tokens_remaining,
        "requests_remaining": status.requests_remaining,
        "tokens_used_pct": status.tokens_used_pct,
        "requests_used_pct": status.requests_used_pct,
        "exhausted": status.exhausted,
    }


def get_quota_status(conn: DictConnection) -> list[dict[str, Any]]:
    """Every provider worth showing remaining-budget for: the two ADR
    §9.1-ruled ones, every admin-defined provider
    (app.services.llm.provider_registry — mistral, or anything an admin
    has added), and any provider that already has a saved quota limit
    even if its definition was later removed. Never a hardcoded 2-vendor
    list — provider identities are runtime config now (Task 3).
    """
    limits = llm_quota.load_provider_limits(conn)
    definitions = load_provider_definitions(conn)
    providers = sorted(set(llm_routing.KNOWN_PROVIDERS) | set(definitions.keys()) | set(limits.keys()))
    tracker = llm_quota.QuotaTracker(store=llm_quota.SettingsQuotaStore(conn=conn), limits=limits)
    return [_quota_status_to_dict(tracker.status(provider)) for provider in providers]


def save_quota_limit(conn: DictConnection, provider: str, *, tpd: int | None, rpd: int | None) -> dict[str, Any]:
    if not provider.strip():
        raise ValidationError("provider must be non-empty")
    limits = llm_quota.load_provider_limits(conn)
    limits[provider] = llm_quota.ProviderLimits(tpd=tpd, rpd=rpd)
    llm_quota.save_provider_limits(conn, limits)
    conn.commit()
    tracker = llm_quota.QuotaTracker(store=llm_quota.SettingsQuotaStore(conn=conn), limits=limits)
    return _quota_status_to_dict(tracker.status(provider))


# ── Generic provider registry: arbitrary providers + pooled credentials
# (ADR §9.1 / owner's Task 3: "admins add API keys for arbitrary
# providers from the dashboard") ─────────────────────────────────────
#
# app.services.llm.provider_registry is imported lazily (function-local),
# same reasoning as _build_client_for_feature above: it's P3-LLM's
# concurrent territory, and this file must stay importable (so plain
# keyword/source CRUD keeps working) even if that package is transiently
# broken mid-edit.


def _definition_to_dict(definition: Any) -> dict[str, Any]:
    return {
        "provider": definition.provider,
        "adapter": definition.adapter,
        "base_url": definition.base_url,
        "default_model": definition.default_model,
        "enabled": definition.enabled,
        "note": definition.note,
    }


def load_provider_definitions(conn: DictConnection) -> dict[str, Any]:
    from app.services.llm.provider_registry import load_provider_definitions as _load

    return _load(conn)


def list_provider_definitions(conn: DictConnection) -> list[dict[str, Any]]:
    definitions = load_provider_definitions(conn)
    return [_definition_to_dict(d) for _, d in sorted(definitions.items())]


def save_provider_definition(
    conn: DictConnection,
    provider: str,
    *,
    adapter: str,
    base_url: str | None,
    default_model: str,
    enabled: bool = True,
    note: str = "",
) -> dict[str, Any]:
    """Admin write path for "add a provider" — a brand-new provider name
    with its own adapter kind (`openai_compatible` needs `base_url`;
    `gemini` does not), default model, and enabled flag. No code change,
    no migration — this is the entire mechanism Task 3 asks for.
    """
    if not provider.strip():
        raise ValidationError("provider must be non-empty")
    from app.services.llm.provider_registry import ProviderDefinition
    from app.services.llm.provider_registry import save_provider_definitions as _save

    definitions = load_provider_definitions(conn)
    definitions[provider] = ProviderDefinition(
        provider=provider, adapter=adapter, base_url=base_url, default_model=default_model, enabled=enabled, note=note
    )  # raises ValidationError for an unknown adapter / missing base_url
    _save(conn, definitions)
    conn.commit()
    return _definition_to_dict(definitions[provider])


def list_provider_credentials(conn: DictConnection, provider: str) -> list[dict[str, Any]]:
    """Masked suffix only (ADR §9.1) — `load_provider_credentials`
    returns DECRYPTED keys for the LLM client's own use; this admin-facing
    listing must never pass that plaintext further than this function.
    """
    from app.logging import mask
    from app.services.llm.provider_registry import load_provider_credentials as _load

    return [
        {"provider": c.provider, "key_id": c.key_id, "api_key_last4": mask(c.api_key)} for c in _load(conn, provider)
    ]


def save_provider_credential(conn: DictConnection, provider: str, key_id: str, api_key: str) -> dict[str, Any]:
    from app.logging import mask
    from app.services.llm.provider_registry import save_provider_credential as _save

    _save(conn, provider, key_id, api_key)  # raises ValidationError if key_id is blank
    conn.commit()
    return {"provider": provider, "key_id": key_id, "api_key_last4": mask(api_key)}


def remove_provider_credential(conn: DictConnection, provider: str, key_id: str) -> None:
    from app.services.llm.provider_registry import remove_provider_credential as _remove

    _remove(conn, provider, key_id)
    conn.commit()


# ═════════════════════════════════════════════════════════════════════
# Storage dashboard + export/import (ADR §9.2, DoD #5/#6)
# ═════════════════════════════════════════════════════════════════════


def storage_usage(conn: DictConnection) -> StorageUsage:
    return get_storage_usage(conn)


def run_purge(
    conn: DictConnection,
    *,
    operation: str,
    older_than_days: int | None = None,
    older_than: date | None = None,
    require_exported: bool = True,
    confirm: bool = False,
) -> PurgeResult:
    """Dispatches to app.services.storage's three purge functions —
    consumed, never reimplemented (ARCH-DB owns the dry-run/confirm
    mechanics and is concurrently fixing a preview/delete race there).
    """
    if operation == "stale_bodies":
        return purge_stale_bodies(
            conn, older_than_days=older_than_days, require_exported=require_exported, confirm=confirm
        )
    if operation == "stale_unmatched":
        return purge_stale_unmatched_articles(conn, older_than_days=older_than_days, confirm=confirm)
    if operation == "matched":
        if older_than is None:
            raise ValidationError("older_than (an ISO date) is required for the 'matched' purge operation")
        return purge_matched_articles(conn, older_than=older_than, require_exported=require_exported, confirm=confirm)
    raise ValidationError(f"unknown purge operation {operation!r} — must be stale_bodies/stale_unmatched/matched")


EXPORT_DIR = "var/exports"


def run_export(
    conn: DictConnection,
    *,
    day_from: date | None = None,
    day_to: date | None = None,
    review_status: str | None = None,
    mark_exported: bool = True,
    path: str | None = None,
) -> ExportManifest:
    if path is None:
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path = f"{EXPORT_DIR}/export_{ts}.jsonl.gz"
    manifest = export_to_jsonl(
        conn, path, day_from=day_from, day_to=day_to, review_status=review_status, mark_exported=mark_exported
    )
    conn.commit()
    return manifest


def run_import(conn: DictConnection, *, path: str) -> ImportReport:
    report = import_from_jsonl(conn, path)
    conn.commit()
    return report


# ═════════════════════════════════════════════════════════════════════
# Source health + pipeline stage runs (frontend-contract.md §3.10)
# ═════════════════════════════════════════════════════════════════════

# The 5 pipeline stages, in the exact order/labels frontend-contract.md
# §3.10 names them. "bodies" (Get article text) has no existing
# orchestrator anywhere in this codebase — legacy itself runs
# `do_bodies=False` on every normal daily run (app.services.enrichment's
# own module docstring), and P1-INGEST's collector never fetches bodies
# either — so it is implemented here, minimally and boundedly, directly
# against the read-only app.sources plugin interface (the same one
# probe_source/diagnose_source above already use). The other four
# dispatch straight to app.services.enrichment.run_llm_enrichment,
# P3-LLM's already-built stage orchestrator — not reimplemented here.
PIPELINE_STAGES: tuple[tuple[str, str, str], ...] = (
    ("translate", "Translate", "Bengali headlines → English"),
    ("bodies", "Get article text", "Fetch the full article body for today's matched stories"),
    ("dedup", "Group duplicates", "Cluster near-duplicate coverage of the same event"),
    ("judge", "Score relevance", "LLM relevance scoring, 1-10"),
    ("topic", "Group topics", "Cluster scored articles into broad topics"),
)
_PIPELINE_STAGE_KEYS = {key for key, _, _ in PIPELINE_STAGES}


def _stage_status_setting_key(stage: str, day: date) -> str:
    return f"pipeline_stage_status:{stage}:{day.isoformat()}"


def _record_stage_status(
    conn: DictConnection, stage: str, *, status: str, detail: str, day: date | None = None
) -> None:
    day = day or date.today()
    set_setting(
        conn,
        _stage_status_setting_key(stage, day),
        json.dumps({"status": status, "detail": detail, "ran_at": datetime.now(timezone.utc).isoformat()}),
    )
    conn.commit()


def pipeline_stage_statuses(conn: DictConnection, day: date | None = None) -> list[dict[str, Any]]:
    day = day or date.today()
    out = []
    for key, name, desc in PIPELINE_STAGES:
        raw = get_setting(conn, _stage_status_setting_key(key, day), default="")
        if raw:
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                data = {}
            out.append(
                {
                    "key": key,
                    "name": name,
                    "desc": desc,
                    "status": data.get("status", "done"),
                    "ran_at": data.get("ran_at"),
                    "detail": data.get("detail", ""),
                }
            )
        else:
            out.append({"key": key, "name": name, "desc": desc, "status": "not_run", "ran_at": None, "detail": ""})
    return out


def _run_bodies_stage(conn: DictConnection, *, limit: int = 100) -> dict[str, int]:
    from app.sources.base import FeedItem
    from app.sources.registry import all_sources

    _ensure_registry_loaded()
    by_display_name = {getattr(cls, "display_name", cls.key): cls for cls in all_sources().values()}

    rows = conn.execute(
        "SELECT id, url, title, source FROM articles "
        "WHERE day = CURRENT_DATE AND review_status = 'pending' AND body_status = 'not_attempted' "
        "ORDER BY id LIMIT %s",
        (limit,),
    ).fetchall()

    fetched = failed = skipped = 0
    for row in rows:
        source_cls = by_display_name.get(row["source"])
        if source_cls is None:
            skipped += 1
            continue
        try:
            instance = source_cls()
            item = FeedItem(url=row["url"], title=row["title"], source_key=getattr(source_cls, "key", ""))
            article = instance.fetch_article(item)
            conn.execute(
                "INSERT INTO article_bodies (article_id, content_text, fetched_at) VALUES (%s, %s, now()) "
                "ON CONFLICT (article_id) DO UPDATE SET content_text = EXCLUDED.content_text, fetched_at = now()",
                (row["id"], article.body_text),
            )
            conn.execute("UPDATE articles SET body_status = 'fetched' WHERE id = %s", (row["id"],))
            fetched += 1
        except SourceError:
            conn.execute("UPDATE articles SET body_status = 'failed' WHERE id = %s", (row["id"],))
            failed += 1
    conn.commit()
    return {"attempted": len(rows), "fetched": fetched, "failed": failed, "skipped_no_plugin": skipped}


def run_pipeline_stage(conn: DictConnection, stage_key: str) -> dict[str, str]:
    if stage_key not in _PIPELINE_STAGE_KEYS:
        raise ValidationError(f"unknown pipeline stage {stage_key!r} — must be one of {sorted(_PIPELINE_STAGE_KEYS)}")
    try:
        if stage_key == "bodies":
            result = _run_bodies_stage(conn)
            detail = (
                f"attempted {result['attempted']}, fetched {result['fetched']}, "
                f"failed {result['failed']}, skipped (no plugin) {result['skipped_no_plugin']}"
            )
        else:
            # Lazy import — see _build_client_for_feature's docstring for
            # why app.services.llm/.enrichment are never imported at
            # module level here: sys.modules caches the module either
            # way, so a test that does `from app.services import
            # enrichment; monkeypatch.setattr(enrichment, ...)` still
            # patches the exact object this resolves to.
            from app.services import enrichment

            enrichment_result = enrichment.run_llm_enrichment(conn, stages={stage_key})
            detail = f"processed {enrichment_result.enriched} article(s)"
            if enrichment_result.skipped_stages:
                detail += f"; skipped (no provider configured): {', '.join(enrichment_result.skipped_stages)}"
    except Exception as exc:
        _record_stage_status(conn, stage_key, status="error", detail=str(exc))
        raise
    _record_stage_status(conn, stage_key, status="done", detail=detail)
    return {"stage": stage_key, "status": "done", "detail": detail}


def run_pipeline_all(conn: DictConnection) -> list[dict[str, str]]:
    """Runs all 5 stages in order. Each stage's failure is isolated —
    one broken stage is recorded and the rest still run — mirroring
    app.services.collector's per-source isolation philosophy at the
    pipeline-stage level instead of the per-source level.
    """
    results = []
    for stage_key, _, _ in PIPELINE_STAGES:
        try:
            results.append(run_pipeline_stage(conn, stage_key))
        except Exception as exc:  # noqa: BLE001 - deliberate per-stage isolation, see docstring
            results.append({"stage": stage_key, "status": "error", "detail": str(exc)})
    return results


def health_sources(conn: DictConnection) -> list[dict[str, Any]]:
    """`healthRows[]` (§3.10) — one row per configured source, joined
    against the most recent collection run's per-source `details` JSONB
    (app.services.collector's `runs.details`, keyed by display name).
    """
    sources_rows = list_sources(conn)
    latest_run = conn.execute("SELECT details FROM runs ORDER BY started_at DESC LIMIT 1").fetchone()
    details: dict[str, Any] = dict(latest_run["details"]) if latest_run and latest_run["details"] else {}

    rows = []
    for source in sources_rows:
        d = details.get(source["name"], {})
        status_raw = d.get("status")
        fetched = int(d.get("fetched", 0))
        inserted = int(d.get("inserted", 0))
        if not source["enabled"]:
            status, note = "disabled", "source disabled"
        elif status_raw == "failed":
            status, note = "error", f"error: {d.get('error', '')}"
        elif status_raw == "ok" and fetched > 0 and inserted == 0:
            status, note = "quiet", f"fetched {fetched} · {fetched} already stored"
        elif status_raw == "ok":
            status, note = "healthy", ""
        elif status_raw == "empty":
            status, note = "quiet", "no articles found this run"
        else:
            status, note = "quiet", "no run data yet"
        rows.append(
            {
                "id": source["id"],
                "name": source["name"],
                "method": source["methods"][0] if source["methods"] else "",
                "fetched": fetched,
                "kept": inserted,
                "status": status,
                "note": note,
            }
        )
    return rows


def health_overview(conn: DictConnection) -> dict[str, Any]:
    """The KPI tiles §3.10 flags as unbound literals in the mock —
    computed for real from `health_sources`/`health_runs`, never a fixed
    display value.
    """
    rows = health_sources(conn)
    latest_run = conn.execute("SELECT started_at, total_matched FROM runs ORDER BY started_at DESC LIMIT 1").fetchone()
    return {
        "healthy": sum(1 for r in rows if r["status"] == "healthy"),
        "needs_attention": sum(1 for r in rows if r["status"] == "error"),
        "fetched_last_run": sum(r["fetched"] for r in rows),
        "matched_last_run": latest_run["total_matched"] if latest_run else 0,
        "last_run_at": latest_run["started_at"].isoformat() if latest_run else None,
        "stages": pipeline_stage_statuses(conn),
    }


def health_runs(conn: DictConnection, limit: int = 5) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT id, started_at, trigger, total_new, total_matched, status FROM runs ORDER BY started_at DESC LIMIT %s",
        (limit,),
    ).fetchall()
    return [
        {
            "id": r["id"],
            "when": r["started_at"].isoformat() if r["started_at"] else "",
            "trigger": r["trigger"],
            "new_count": r["total_new"],
            "matched_count": r["total_matched"],
            "status": r["status"],
        }
        for r in rows
    ]
