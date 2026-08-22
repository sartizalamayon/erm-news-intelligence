-- 0025_rollup_econ.sql
--
-- The economy half of the risk board, trended. Same reason
-- 0021_watchlist_and_coverage_rollups.sql exists: the long-term
-- dashboard tab reads ONLY rollups, never raw `articles`, so anything
-- that tab shows needs a bounded monthly table behind it. This table
-- keeps that property -- its size is (months x 6 axes) and is
-- independent of article volume, so a year of history is 72 rows.
--
-- ── rollup_monthly_econ ─────────────────────────────────────────────
-- 0021 gave a Chief Risk Officer "what is the regulator doing to us"
-- and "who else is in the news". It stopped deliberately at the
-- telecom boundary -- app/db/watchlist.py's docstring argues that
-- "Geopolitical" and "Macroeconomy" are not ERM register line items,
-- which is right about those two words and wrong as a description of
-- what a Bangladeshi operator actually carries. The exposures are
-- specific and quantified, not atmospheric:
--
--   energy          tower and network opex -- grid tariff, diesel for
--                   the backup fleet, load-shedding hours
--   fx              capex and spectrum fees are dollar-denominated
--                   while revenue is in taka
--   revenue_tax     handset and equipment duty arrives via NBR and the
--                   budget; sector-specific duty follows this line
--   banking         where receivables and the MFS float sit
--   trade_rmg       the export base that funds subscriber ARPU
--   capital_markets listed-company notices and BSEC actions
--
-- The column shape is deliberately IDENTICAL to rollup_monthly_watch,
-- minus the group_key that the flat six-axis vocabulary does not need.
-- The two tables are read side by side on one screen, so a telecom line
-- and an economy line have to be directly comparable: same counting
-- rule, same `llm_score >= 8` critical boundary as
-- rollup_monthly_score_band, same "store scored_count and score_sum
-- rather than a precomputed average" so a mean can be derived AND
-- qualified by how many articles it rests on.
--
-- Why a fixed axis vocabulary rather than `articles.topic_tag`: exactly
-- 0021's argument. topic_tag is free text an LLM writes per article
-- (one month produced 111 distinct values, three of them spellings of
-- "Middle East / war"). You cannot trend a theme whose name changes.
-- The six axis keys live in app/db/econaxes.py and mean the same thing
-- in every month of the series.
--
-- Rows are written for EVERY axis every month, whether or not that
-- month matched any -- so the series has no holes and a chart can tell
-- "zero coverage" from "no row". See app/db/rollups.py::_upsert_econ.
CREATE TABLE rollup_monthly_econ (
    month          DATE NOT NULL,
    axis           TEXT NOT NULL,   -- app/db/econaxes.py EconAxis.key, e.g. 'energy'
    article_count  INTEGER NOT NULL DEFAULT 0,   -- headlines identified as being on this axis
    scored_count   INTEGER NOT NULL DEFAULT 0,   -- of those, llm_score IS NOT NULL
    high_count     INTEGER NOT NULL DEFAULT 0,   -- of those, llm_score >= 8 (same boundary as rollup_monthly_score_band)
    score_sum      NUMERIC NOT NULL DEFAULT 0,   -- sum of llm_score over scored_count, so a mean can be derived AND qualified
    PRIMARY KEY (month, axis)
);
-- Intended query: "12 months of energy exposure and how severe it was" --
--   SELECT * FROM rollup_monthly_econ WHERE month >= ? ORDER BY axis, month;
