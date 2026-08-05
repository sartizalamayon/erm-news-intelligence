-- 0003_articles.sql
--
-- The core article entity. Carries forward every legacy column that
-- still has meaning (legacy-behavior.md §6); see storage-policy.md
-- "Schema decisions" for the full carry-forward / drop list. Article
-- BODY TEXT (`content_text`) deliberately lives in its own table
-- (0004_article_bodies_and_seen_urls.sql) — see storage-policy.md §2 for
-- why: it is the single heaviest field and the one the retention policy
-- sheds on a short window, and keeping it out of this table means
-- shedding it is a plain DELETE on a small child table (fast, clean
-- VACUUM) rather than an UPDATE ... SET content_text = '' that leaves
-- dead tuples in the wide, hot `articles` table.
--
-- Dropped vs. legacy (see legacy-behavior.md §7 and §5.5 for the
-- evidence):
--   * `summary` — confirmed dead column, zero callers anywhere in the
--     legacy codebase, always ''. Not carried forward.
--   * `match_type = 'both'` — referenced in legacy's WHERE-clause filters
--     but never assigned by any code path (grep-confirmed, ADR §6).
--     The CHECK constraint below only allows the values actually ever
--     written ('', 'keyword', 'semantic'), so this dead value cannot
--     resurface in the new schema even by accident.
--   * `semantic_score` / `match_type = 'semantic'` themselves ARE kept
--     (not dropped) despite the semantic-recall *pipeline stage* being
--     retired (ADR §6/§1: 4 of 51,886 rows ever promoted, "stripping it
--     is overdue") — that ruling is about not rebuilding the write path
--     (semantic.py), not about discarding the 4 rows of real historical
--     data those columns hold. Carrying the column forward costs
--     nothing and keeps the legacy migration lossless (DoD #1: "carry
--     forward every legacy column that still has meaning").

CREATE TABLE articles (
    id               BIGSERIAL PRIMARY KEY,
    url              TEXT NOT NULL,
    url_norm         TEXT NOT NULL DEFAULT '',
    source           TEXT NOT NULL,
    title            TEXT NOT NULL,
    title_en         TEXT NOT NULL DEFAULT '',
    published        TIMESTAMPTZ,             -- NULL = undated (legacy: published = ''). Dhaka has a fixed +06:00 offset, no DST, so this round-trips losslessly to/from legacy's "%Y-%m-%d %H:%M" Dhaka-local string.
    fetched_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    day              DATE NOT NULL,           -- the COLLECTION day (run start), not necessarily the article's published day — same semantics as legacy (legacy-behavior.md §6).
    sections         TEXT NOT NULL DEFAULT '',
    keywords         TEXT NOT NULL DEFAULT '',
    dup_group        BIGINT,                  -- cluster id, locally scoped per run per legacy semantics (legacy-behavior.md §6) — NOT a global identity, no FK by design.
    is_primary       BOOLEAN NOT NULL DEFAULT TRUE,
    also_in          TEXT NOT NULL DEFAULT '',
    review_status    TEXT NOT NULL DEFAULT 'pending' CHECK (review_status IN ('pending', 'filtered_out', 'approved', 'rejected')),
    reviewed_by      TEXT NOT NULL DEFAULT '',
    reviewed_at      TIMESTAMPTZ,
    llm_score        REAL,
    llm_reason       TEXT NOT NULL DEFAULT '',
    match_type       TEXT NOT NULL DEFAULT '' CHECK (match_type IN ('', 'keyword', 'semantic')),
    semantic_score   REAL,
    topic_tag        TEXT NOT NULL DEFAULT '',
    topic_standout   BOOLEAN NOT NULL DEFAULT FALSE,
    topic_parent_id  BIGINT REFERENCES articles (id) ON DELETE SET NULL,
    body_status      TEXT NOT NULL DEFAULT 'not_attempted' CHECK (body_status IN ('not_attempted', 'fetched', 'failed', 'purged')),
                     -- NEW vs. legacy, resolves frontend-contract.md §8 open question #10 ("body missing vs. genuinely
                     -- empty" — legacy collapses both to `content_text = ''`). Also the mechanism the retention
                     -- policy needs to tell "never fetched" apart from "fetched, then purged for space" after a
                     -- content_text row is deleted from article_bodies (storage-policy.md §2).
    exported_at      TIMESTAMPTZ,             -- NEW. Set when this row was last included in a completed export
                     -- (app/services/data_transfer.py). Backs DoD #5's "purge must never delete anything not yet
                     -- exported if the operator asked for export-first" — purge's `require_exported` gate filters on
                     -- this column rather than inventing a separate ledger table.
    search_vec       tsvector GENERATED ALWAYS AS (
                         to_tsvector('simple', coalesce(title, '') || ' ' || coalesce(title_en, ''))
                     ) STORED
);

-- ---------------------------------------------------------------------
-- Indexes — each with the query it exists for. This table is where the
-- legacy system's one measured bottleneck lived (ADR §2: `EXPLAIN QUERY
-- PLAN` on `WHERE url=? OR url_norm=?` returned `SCAN articles` — a full
-- table scan on every fetched article, ~437M row comparisons in run
-- #110 alone) and it is not reproduced here.
-- ---------------------------------------------------------------------

-- Fix for the flagged bottleneck (ADR §2 point 1, §6). Intended query:
--   INSERT INTO articles (...) VALUES (...)
--   ON CONFLICT (url) DO NOTHING RETURNING id;
-- Raw-URL identity / integrity backstop — a source literally re-fetching
-- an URL it already stored.
CREATE UNIQUE INDEX idx_articles_url ON articles (url);

-- The dedup-on-insert fix proper. Intended query (see app/db/queries.py's
-- insert_article — the `WHERE url_norm <> ''` on the conflict target is
-- required for Postgres to match this PARTIAL unique index, not optional
-- decoration):
--   INSERT INTO articles (...) VALUES (...)
--   ON CONFLICT (url_norm) WHERE url_norm <> '' DO NOTHING RETURNING id;
-- Replaces legacy's `SELECT 1 FROM articles WHERE url=? OR url_norm=?`
-- full scan with a single B-tree lookup baked into the INSERT itself —
-- O(log n) and race-safe under the concurrent Phase-1-fetch /
-- Phase-2-write model, instead of a separate SELECT-then-INSERT.
-- Partial (`WHERE url_norm <> ''`) so a hypothetical unset/blank
-- url_norm never collides across unrelated rows.
CREATE UNIQUE INDEX idx_articles_url_norm ON articles (url_norm) WHERE url_norm <> '';

-- Intended query: KPI/funnel/14-day-volume dashboard reads scoped to a
-- day or a recent day range (frontend-contract.md §3.1) —
--   SELECT day, count(*) FROM articles WHERE day >= ? GROUP BY day;
CREATE INDEX idx_articles_day ON articles (day);

-- Intended query: today's review queue (legacy-behavior.md §2.7's
-- `day=today AND review_status='pending'`; frontend-contract.md §3.3
-- list[]) and the funnel's per-phase counts —
--   SELECT * FROM articles WHERE day = ? AND review_status = ?;
CREATE INDEX idx_articles_day_status ON articles (day, review_status);

-- Intended query: the global-shell `pendingCount` badge
-- (frontend-contract.md §2), read on every authenticated page —
--   SELECT count(*) FROM articles WHERE review_status = 'pending';
-- Partial on the one status value that's actually queried globally
-- (unlike day-scoped 'approved'/'filtered_out' reads, which the
-- (day, review_status) index above already covers).
CREATE INDEX idx_articles_pending ON articles (review_status) WHERE review_status = 'pending';

-- Intended query: "most productive sources today" (frontend-contract.md
-- §3.1 topSources[]) —
--   SELECT source, count(*) FROM articles WHERE day = ? GROUP BY source;
CREATE INDEX idx_articles_source_day ON articles (source, day);

-- Intended query: expand a duplicate-outlet group (frontend-contract.md
-- §3.3 curDups[]) —
--   SELECT * FROM articles WHERE dup_group = ?;
CREATE INDEX idx_articles_dup_group ON articles (dup_group) WHERE dup_group IS NOT NULL;

-- Intended query: topic/theme rollups and the topic-peers panel
-- (frontend-contract.md §3.2 heatRows[], §3.3 curTopicPeers[]) —
--   SELECT * FROM articles WHERE topic_tag = ? AND day >= ?;
CREATE INDEX idx_articles_topic_tag ON articles (topic_tag) WHERE topic_tag <> '';

-- Intended query: Search archive (frontend-contract.md §3.6) full-text
-- match over headline + English translation —
--   SELECT * FROM articles WHERE search_vec @@ plainto_tsquery('simple', ?);
-- `simple` config (no English/Bengali-specific stemming) deliberately —
-- the corpus is 72.4% Bengali-titled (ADR §3) and Postgres ships no
-- Bengali text-search dictionary; `simple` tokenizes/matches without
-- language-specific stemming, which is correct for both scripts rather
-- than mis-stemming one of them. Revisit if search recall quality turns
-- out to need more (e.g. pg_trgm) — flagged, not built, since nothing
-- in the DoD requires fuzzy search and pg_trgm is a new extension to
-- justify.
CREATE INDEX idx_articles_search_vec ON articles USING GIN (search_vec);

-- Intended query: `require_exported` purge gate (DoD #5) —
--   DELETE FROM articles WHERE day < ? AND exported_at IS NOT NULL;
-- Partial since most rows are never exported.
CREATE INDEX idx_articles_exported_at ON articles (exported_at) WHERE exported_at IS NOT NULL;
