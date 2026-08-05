-- 0002_reference_tables.sql
--
-- sources, keywords, runs — small, slow-changing reference/control
-- tables carried forward 1:1 from legacy (legacy-behavior.md §2.1,
-- §4.1, §3), Postgres-typed (JSON -> JSONB, INTEGER flags -> BOOLEAN,
-- TEXT timestamps -> TIMESTAMPTZ).

CREATE TABLE sources (
    id          BIGSERIAL PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    method      TEXT NOT NULL,
    config      JSONB NOT NULL DEFAULT '{}'::jsonb,
    language    TEXT NOT NULL DEFAULT 'mixed',
    enabled     BOOLEAN NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- No secondary index: 51 rows (legacy count), read in full at pipeline
-- start (`_sources_from_db()`) and on the admin Sources screen. `UNIQUE
-- (name)` is the only lookup that matters (sync_sources() upsert-by-name).

CREATE TABLE keywords (
    id          BIGSERIAL PRIMARY KEY,
    section     TEXT NOT NULL,
    term_en     TEXT NOT NULL DEFAULT '',
    term_bn     TEXT NOT NULL DEFAULT '',
    enabled     BOOLEAN NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- Intended query: "load enabled keywords grouped by section" at pipeline
-- start and on the admin Keywords screen (frontend-contract.md §3.8,
-- kwSectionTabs[]) — WHERE enabled GROUP BY section. 271 rows (legacy
-- count) is small enough that a full scan is fine, but the partial
-- index keeps the common case (enabled-only) cheap and self-documents
-- the query shape as sections grow.
CREATE INDEX idx_keywords_section_enabled ON keywords (section) WHERE enabled;

CREATE TABLE runs (
    id            BIGSERIAL PRIMARY KEY,
    started_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at   TIMESTAMPTZ,
    heartbeat_at  TIMESTAMPTZ,
    status        TEXT NOT NULL DEFAULT 'running' CHECK (status IN ('running', 'done', 'error')),
    trigger       TEXT NOT NULL DEFAULT 'manual' CHECK (trigger IN ('manual', 'scheduled')),
    total_new     INTEGER NOT NULL DEFAULT 0,
    total_matched INTEGER NOT NULL DEFAULT 0,
    details       JSONB NOT NULL DEFAULT '{}'::jsonb
);
-- Intended query: "most recent runs" (Source health screen run history,
-- frontend-contract.md §3.10 runRows[]; legacy-behavior.md §3's
-- `ORDER BY id DESC LIMIT 10`) -> ORDER BY started_at DESC LIMIT n.
CREATE INDEX idx_runs_started_at ON runs (started_at DESC);
-- Intended query: batch-job watchdog "find runs stuck at running" ->
-- WHERE status = 'running'. Legacy has zero orphan-detection (16 of 110
-- runs, 14.5%, are permanently stuck at status='running' with no
-- finished_at — legacy-behavior.md §3). `heartbeat_at` (updated
-- periodically by the batch job, out of this migration's scope to
-- populate) lets a watchdog distinguish "still running" from "the
-- process died" via `heartbeat_at < now() - interval` instead of never
-- being able to tell the difference, which is the mechanism legacy
-- lacked.
CREATE INDEX idx_runs_status_running ON runs (status) WHERE status = 'running';
