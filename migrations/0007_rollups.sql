-- 0007_rollups.sql
--
-- Pre-computed rollup/aggregate tables — the mechanism that makes
-- frontend-contract.md §3.2's 12-month long-term dashboard tab
-- affordable under the 500 MB ceiling (ADR §4, storage-policy.md §2).
--
-- The key property of every table below: its size is bounded by
-- (months x a small fixed cardinality — sections, sources, score
-- bands, themes), NOT by article volume. A year of history across all
-- five rollup tables is on the order of a few thousand rows total
-- (measured cost: see storage-policy.md §3 "rollup cost per year") —
-- independent of whether the underlying `articles` rows that produced
-- those counts have since been purged. This is what lets raw article
-- detail be purged on a short-to-medium window (storage-policy.md §2)
-- without the long-term dashboard losing any of its 12-month history:
-- the dashboard never queries raw `articles` for anything older than
-- the raw-retention window, only these tables.
--
-- Grain is monthly (first-of-month DATE) because frontend-contract.md
-- §3.2 asks for 12 *monthly* points on every long-term series
-- (months12[], heatRows[].cells[], trendCards[].points). The 14-day
-- short-term chart (§3.1 days14[]) does NOT need a table here — it
-- always queries a window inside `raw_retention_days` (0001_settings.sql
-- default 90 days > 14), so it reads `articles` directly; adding a
-- daily rollup table for a window that's always live in the source
-- table would be duplicated state for no storage benefit (GATE 0:
-- lean, no ceremony beyond what's needed).
--
-- Population is NOT this migration's job — these tables are written by
-- whichever agent owns the dashboard/analytics write path (folding a
-- day's collected articles into the current month's row, or a nightly
-- rollup job), using UPSERT-style writes, e.g.:
--   INSERT INTO rollup_monthly_section (month, section, fetched_count, matched_count)
--   VALUES (?, ?, ?, ?)
--   ON CONFLICT (month, section) DO UPDATE SET
--     fetched_count = rollup_monthly_section.fetched_count + EXCLUDED.fetched_count,
--     matched_count = rollup_monthly_section.matched_count + EXCLUDED.matched_count;
-- ARCH-DB's scope is the storage shape and the retention math it
-- enables, not the analytics/dashboard business logic itself.

CREATE TABLE rollup_monthly_section (
    month           DATE NOT NULL,   -- first day of the month, e.g. 2026-08-01
    section         TEXT NOT NULL,   -- one of the 5 fixed risk sections
    fetched_count   INTEGER NOT NULL DEFAULT 0,
    matched_count   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (month, section)
);
-- Intended query: "coverage volume by section, 12 months"
-- (frontend-contract.md §3.2 months12[].segs[]) —
--   SELECT * FROM rollup_monthly_section WHERE month >= ? ORDER BY month;

CREATE TABLE rollup_monthly_source (
    month           DATE NOT NULL,
    source          TEXT NOT NULL,
    fetched_count   INTEGER NOT NULL DEFAULT 0,
    matched_count   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (month, source)
);
-- Intended query: a 12-month equivalent of the short-term "most
-- productive sources" breakdown, and Source health's per-source
-- long-run trend —
--   SELECT * FROM rollup_monthly_source WHERE source = ? ORDER BY month;

CREATE TABLE rollup_monthly_score_band (
    month          DATE NOT NULL,
    band           TEXT NOT NULL CHECK (band IN ('high', 'medium', 'low', 'least')),  -- same 4-tier taxonomy as relMix[] (frontend-contract.md §3.1): >=8 / 6-7 / =5 / <5.
    article_count  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (month, band)
);
-- Intended query: a 12-month "relevance mix" trend (not explicitly a
-- named binding in §3.2, but the natural long-term extension of §3.1's
-- relMix[] and directly answers "score distributions" from the mission
-- brief's DoD #3) —
--   SELECT * FROM rollup_monthly_score_band WHERE month >= ? ORDER BY month, band;

CREATE TABLE rollup_monthly_theme (
    month          DATE NOT NULL,
    theme          TEXT NOT NULL,     -- articles.topic_tag value, e.g. "Middle East / oil".
    article_count  INTEGER NOT NULL DEFAULT 0,
    intensity      NUMERIC(4, 3) NOT NULL DEFAULT 0,  -- article_count normalized 0-1 against that theme's (or that month's — see frontend-contract.md §8 open question 7) max; whichever normalization the dashboard agent picks, this column is where it lands.
    PRIMARY KEY (month, theme)
);
-- Intended query: the theme-intensity heatmap (frontend-contract.md
-- §3.2 heatRows[].cells[]) —
--   SELECT * FROM rollup_monthly_theme WHERE month >= ? ORDER BY theme, month;
-- Also backs "standing themes" (§3.2 standing[]): a theme with
-- article_count > 0 in N consecutive recent months, computed as a plain
-- query over this table (not a separate stored table — deriving it at
-- read time from ~60-300 rows/year is cheap and avoids a second source
-- of truth for the same underlying counts):
--   SELECT theme, sum(article_count), count(*) FILTER (WHERE article_count > 0)
--   FROM rollup_monthly_theme WHERE month >= ? GROUP BY theme;

CREATE TABLE macro_indicator_values (
    indicator  TEXT NOT NULL CHECK (indicator IN ('taka_usd', 'inflation', 'brent_crude', 'remittance')),  -- the fixed 4-indicator watchlist, frontend-contract.md §3.2 trendCards[].
    month      DATE NOT NULL,
    value      NUMERIC NOT NULL,
    PRIMARY KEY (indicator, month)
);
-- Intended query: the macro trend cards (frontend-contract.md §3.2
-- trendCards[].points, 12-value series) —
--   SELECT * FROM macro_indicator_values WHERE indicator = ? ORDER BY month;
-- Note: unlike every other table in this migration, this one has NO
-- relationship to `articles` at all — Taka/USD, inflation, Brent crude,
-- and remittance figures are external economic data, not derived from
-- the collected corpus. It is admin-entered or ingested from an
-- external feed (out of ARCH-DB's scope to populate); it is listed here
-- only because frontend-contract.md §3.2 requires it and it needs a
-- home. Storage cost is negligible (4 indicators x 12 months/year = 48
-- rows/year).
