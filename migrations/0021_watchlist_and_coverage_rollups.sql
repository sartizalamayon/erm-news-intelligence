-- 0021_watchlist_and_coverage_rollups.sql
--
-- Two more monthly rollups, added for the same reason 0007's five
-- exist: the long-term dashboard tab reads ONLY rollups, never raw
-- `articles`, so anything that tab wants to show has to have a bounded
-- monthly table behind it. Both tables below keep that property. Their
-- size is (months x a small fixed cardinality) and is independent of
-- article volume, so a year of history is a few hundred rows.
--
-- ── rollup_monthly_watch ────────────────────────────────────────────
-- 0007's tables answer "how much did we collect, sliced by section /
-- source / score / free-text theme". None of those is a question a
-- Chief Risk Officer at a mobile operator asks. The questions actually
-- asked of this screen are "what is the regulator doing to us", "who
-- else is in the news and for what", and "is this getting worse" --
-- which need coverage counted per NAMED RISK ENTITY (BTRC, spectrum,
-- Robi, ...), not per section.
--
-- Why a new table rather than reusing rollup_monthly_theme: that table
-- is keyed on `articles.topic_tag`, which is free text an LLM writes
-- per article. In the real corpus one month alone produced 111 distinct
-- values including "Middle East / war", "Middle East / War" and
-- "Middle East / Conflict" as three separate themes. Useful as a rough
-- heatmap, useless as a risk register: you cannot trend a theme whose
-- name changes. The watchlist entities are a fixed vocabulary defined
-- in app/db/watchlist.py, so a row here means the same thing in every
-- month of the series.
--
-- The severity columns exist so the screen can show ESCALATION rather
-- than volume alone. A theme whose article count doubled is noise if
-- the new articles all scored 2; the same theme is a board item if the
-- average score went up with it. Storing scored_count and score_sum
-- (rather than a precomputed average) keeps the row additive-safe and
-- lets the reader be told how many articles the average rests on --
-- an average over 2 scored articles should not be drawn as a trend.
CREATE TABLE rollup_monthly_watch (
    month          DATE NOT NULL,
    group_key      TEXT NOT NULL,   -- app/db/watchlist.py WatchGroup.key, e.g. 'operators'
    entity         TEXT NOT NULL,   -- app/db/watchlist.py WatchEntity.key, e.g. 'btrc'
    article_count  INTEGER NOT NULL DEFAULT 0,   -- headlines identified as mentioning this entity
    matched_count  INTEGER NOT NULL DEFAULT 0,   -- of those, review_status != 'filtered_out'
    scored_count   INTEGER NOT NULL DEFAULT 0,   -- of those, llm_score IS NOT NULL
    high_count     INTEGER NOT NULL DEFAULT 0,   -- of those, llm_score >= 8 (same band boundary as rollup_monthly_score_band)
    score_sum      NUMERIC NOT NULL DEFAULT 0,   -- sum of llm_score over scored_count, so a mean can be derived AND qualified
    PRIMARY KEY (month, group_key, entity)
);
-- Intended query: "12 months of BTRC coverage and how severe it was" --
--   SELECT * FROM rollup_monthly_watch WHERE month >= ? ORDER BY group_key, entity, month;

-- ── rollup_monthly_coverage ─────────────────────────────────────────
-- The honesty layer, and the reason the rest of this screen can be
-- believed at all.
--
-- `articles.day` is the PUBLICATION date (0018), not the collection
-- date. A crawler that started running in month N still discovers
-- backdated articles published in months N-1, N-2, ... So a 12-month
-- chart keyed on `day` silently mixes two completely different things:
-- months that were actively monitored, and months represented only by
-- whatever old links happened to surface later. In this deployment that
-- is not a hypothetical -- every one of the ~65k articles was fetched
-- in just two calendar months, yet the corpus carries publication dates
-- going back to 2016. Drawn as a plain bar chart that reads as a
-- 100-fold surge in risk coverage. It is not. It is the day monitoring
-- switched on.
--
-- collected_count answers this per month: how many of that month's
-- articles were fetched DURING that month, i.e. observed live rather
-- than recovered afterwards. collected_days answers the blunter version
-- -- on how many distinct days did the collector run at all inside this
-- calendar month. A month with collected_days = 0 is not a quiet month;
-- it is a month we were not watching, and the dashboard is required to
-- say so instead of drawing an empty bar that looks like a measurement.
CREATE TABLE rollup_monthly_coverage (
    month            DATE NOT NULL PRIMARY KEY,
    article_count    INTEGER NOT NULL DEFAULT 0,   -- articles published in this month (any fetch date)
    matched_count    INTEGER NOT NULL DEFAULT 0,   -- of those, review_status != 'filtered_out'
    scored_count     INTEGER NOT NULL DEFAULT 0,   -- of those, llm_score IS NOT NULL
    collected_count  INTEGER NOT NULL DEFAULT 0,   -- of those, ALSO fetched inside this same calendar month
    collected_days   INTEGER NOT NULL DEFAULT 0    -- distinct dates inside this month on which the collector fetched anything at all
);
-- Intended query: "which of the last 12 months were actually monitored" --
--   SELECT * FROM rollup_monthly_coverage WHERE month >= ? ORDER BY month;
