"""
database.py — the app's database layer
=======================================
This file defines the SQLite database and its tables, and gives the rest of
the app simple functions to open a connection.

Think of the database as the shared notebook that BOTH the collector (which
writes articles into it) and the web pages (which read articles and record
approve/reject decisions) use. Everything flows through here.

Five tables:
  sources   — every news source (moved OUT of code, so you can add/edit in UI)
  keywords  — every keyword (English + Bengali), editable in the UI
  articles  — every collected article, with its review status
  users     — login accounts with roles (admin / reviewer)
  runs      — a log of each collection run, for the debug/status dashboard
"""

import sqlite3
from pathlib import Path

# The database file lives in the data/ folder next to the backend.
DB_PATH = Path(__file__).resolve().parent.parent / "data" / "news.db"


def get_connection():
    """Open a connection to the database.
    `row_factory = sqlite3.Row` lets us read columns by name (row["title"])
    instead of by number (row[2]) — much clearer to work with.
    `check_same_thread=False` lets the background collection thread use the DB
    safely; `timeout` makes writers wait briefly instead of erroring if the DB
    is momentarily locked by another connection."""
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=10)
    conn.row_factory = sqlite3.Row
    # WAL mode allows a reader and a writer at the same time (UI + collection)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    """Create all tables if they don't already exist.
    Safe to run repeatedly — it only creates what's missing."""
    conn = get_connection()
    cur = conn.cursor()

    # ── sources ────────────────────────────────────────────────────────────
    # One row per news source. The 'config' column stores method-specific
    # details (URLs, fallback feeds) as JSON text, so different methods can
    # store different shapes without needing different columns.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS sources (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT NOT NULL UNIQUE,
            method      TEXT NOT NULL,          -- sitemap / rss / rss_multi / gnews / page_dates / combine
            config      TEXT NOT NULL DEFAULT '{}',  -- JSON: urls, fallbacks, etc.
            language    TEXT DEFAULT 'mixed',   -- en / bn / mixed
            enabled     INTEGER NOT NULL DEFAULT 1,  -- 1 = active, 0 = disabled
            created_at  TEXT DEFAULT (datetime('now'))
        )
    """)

    # ── keywords ───────────────────────────────────────────────────────────
    # One row per keyword. A keyword can have an English term, a Bengali term,
    # or both. 'section' is the report category (Telco/regulatory, etc.).
    cur.execute("""
        CREATE TABLE IF NOT EXISTS keywords (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            section     TEXT NOT NULL,
            term_en     TEXT DEFAULT '',
            term_bn     TEXT DEFAULT '',
            enabled     INTEGER NOT NULL DEFAULT 1,
            created_at  TEXT DEFAULT (datetime('now'))
        )
    """)

    # ── articles ───────────────────────────────────────────────────────────
    # One row per collected article. 'review_status' is the heart of the
    # review workflow: pending -> approved/rejected.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS articles (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            url           TEXT NOT NULL UNIQUE,
            url_norm      TEXT DEFAULT '',      -- normalized url for cross-method dedup
            source        TEXT NOT NULL,
            title         TEXT NOT NULL,
            published     TEXT DEFAULT '',
            fetched_at    TEXT DEFAULT (datetime('now')),
            day           TEXT,                 -- YYYY-MM-DD for grouping by day
            sections      TEXT DEFAULT '',      -- matched report sections
            keywords      TEXT DEFAULT '',      -- matched keywords
            dup_group     INTEGER DEFAULT NULL, -- dedup cluster id
            is_primary    INTEGER DEFAULT 1,    -- 1 = representative of its dup group
            also_in       TEXT DEFAULT '',      -- other outlets with same story
            review_status TEXT DEFAULT 'pending', -- pending / approved / rejected
            reviewed_by   TEXT DEFAULT '',
            reviewed_at   TEXT DEFAULT '',
            llm_score     REAL DEFAULT NULL,    -- LLM relevance score 0–1 (if enabled)
            llm_reason    TEXT DEFAULT '',      -- LLM's short reason
            summary       TEXT DEFAULT ''       -- LLM one-line summary (if enabled)
        )
    """)

    # ── users ──────────────────────────────────────────────────────────────
    # Login accounts. Passwords are stored HASHED (never plain text) — the
    # auth module handles that. role controls access (admin vs reviewer).
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            username      TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            role          TEXT NOT NULL DEFAULT 'reviewer',  -- admin / reviewer
            created_at    TEXT DEFAULT (datetime('now'))
        )
    """)

    # ── runs ───────────────────────────────────────────────────────────────
    # One row per collection run, for the debug/status dashboard. Per-source
    # detail is stored as JSON in 'details'.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS runs (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at    TEXT DEFAULT (datetime('now')),
            finished_at   TEXT DEFAULT '',
            status        TEXT DEFAULT 'running',  -- running / done / error
            trigger       TEXT DEFAULT 'manual',   -- manual / scheduled
            total_new     INTEGER DEFAULT 0,
            total_matched INTEGER DEFAULT 0,
            details       TEXT DEFAULT '{}'        -- JSON: per-source results
        )
    """)

    # key/value settings (e.g. the editable LLM relevance guidance)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key   TEXT PRIMARY KEY,
            value TEXT DEFAULT ''
        )
    """)

    # ── migrate existing databases: add new columns if they're missing ──
    # (safe to run repeatedly; ignores "already exists" errors)
    existing_cols = [r[1] for r in cur.execute("PRAGMA table_info(articles)").fetchall()]
    for col, decl in [("llm_score", "REAL DEFAULT NULL"),
                      ("llm_reason", "TEXT DEFAULT ''"),
                      ("summary", "TEXT DEFAULT ''"),
                      ("url_norm", "TEXT DEFAULT ''"),
                      ("title_en", "TEXT DEFAULT ''"),
                      ("content_text", "TEXT DEFAULT ''"),
                      ("match_type", "TEXT DEFAULT ''"),
                      ("semantic_score", "REAL DEFAULT NULL"),
                      ("topic_tag", "TEXT DEFAULT ''"),
                      ("topic_standout", "INTEGER DEFAULT 0"),
                      ("topic_parent_id", "INTEGER DEFAULT NULL")]:
        if col not in existing_cols:
            cur.execute(f"ALTER TABLE articles ADD COLUMN {col} {decl}")

    conn.commit()
    conn.close()
    print(f"Database ready at: {DB_PATH}")


def sync_sources():
    """Sync the SOURCES list from collector_logic.py into the DB sources table.
    - NEW sources (by name) are inserted as enabled.
    - EXISTING sources have their method and config UPDATED to match the code
      (so upgrades like sitemap→api take effect), but enabled/disabled state
      is preserved.
    - Sources in the DB that are NOT in the code list are left untouched
      (they may have been added manually via the UI).
    This runs at startup so code-level source changes take effect automatically.
    """
    import json as _json
    try:
        from collector_logic import SOURCES
    except ImportError:
        print("[sync_sources] collector_logic not importable — skipping sync")
        return

    conn = get_connection()
    existing = {}
    for row in conn.execute("SELECT name, method, config FROM sources").fetchall():
        existing[row["name"]] = row

    added = updated = 0
    for src in SOURCES:
        name = src["name"]
        method = src["method"]
        # everything except name and method goes into the config JSON
        cfg = {k: v for k, v in src.items() if k not in ("name", "method")}
        cfg_json = _json.dumps(cfg, ensure_ascii=False)

        # detect language from name/url heuristics
        lang = "mixed"
        name_lower = name.lower()
        if any(w in name_lower for w in ("english", "bbc", "cnn", "ndtv",
                                          "new age", "times of bangladesh",
                                          "al jazeera")):
            lang = "en"
        elif any(w in name_lower for w in ("protidin", "rupantor", "patrika",
                                            "sangbad", "inqilab", "diganta",
                                            "arthosuchak", "nagorik",
                                            "economy post", "bonik")):
            lang = "bn"

        if name not in existing:
            conn.execute(
                "INSERT INTO sources (name, method, config, language, enabled) "
                "VALUES (?, ?, ?, ?, 1)",
                (name, method, cfg_json, lang))
            added += 1
        else:
            # update method and config to match code (preserves enabled state)
            old = existing[name]
            if old["method"] != method or old["config"] != cfg_json:
                conn.execute(
                    "UPDATE sources SET method=?, config=? WHERE name=?",
                    (method, cfg_json, name))
                updated += 1

    conn.commit()
    conn.close()
    if added or updated:
        print(f"[sync_sources] {added} added, {updated} updated "
              f"(total in code: {len(SOURCES)})")


if __name__ == "__main__":
    # Running this file directly sets up the database.
    init_db()
