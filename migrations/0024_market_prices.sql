-- 0024_market_prices.sql
--
-- Storage for the "Market" dashboard tab: Dhaka Stock Exchange prices
-- for the two listed mobile operators this platform's reader actually
-- carries risk on -- Grameenphone (trading code GP, scrip 27001) and
-- Robi Axiata (ROBI, 27003). Written by app/services/market.py, which
-- scrapes dsebd.org (the DSE publishes no API; see that module's
-- docstring for why scraping is the only option and what it costs).
--
-- WHY STORE THIS AT ALL rather than fetching on every page view:
--
--  1. dsebd.org is the single source and it is not fast, not always up,
--     and not rate-limit-documented. The rest of this codebase already
--     takes the position that an external source being down must never
--     break a request path (app/services/macro.py's "never raises"
--     contract). For a single scalar like the USD rate, returning None
--     and hiding a card is an acceptable degradation. For a 12-month
--     price series it is not -- the chart is the tab -- so the series
--     has to survive in our own database and the fetch has to be a
--     background refresh, not a request-path dependency.
--
--  2. The day-end archive is the ONLY place a close for a past day can
--     be recovered from, and it is a bounded window. Once a day scrolls
--     out of the archive's practical range, an unstored close is gone.
--     This table is therefore the long-term record, not a cache.
--
--  3. It is small and stays small by construction: ~250 trading days a
--     year x 2 codes. Ten years of history is ~5,000 rows. There is no
--     retention or partitioning problem to design around here.
--
-- Both tables below are deliberately keyed so a re-run is a no-op:
-- app/services/market.py upserts with ON CONFLICT ... DO UPDATE (the
-- same convention as app/db/rollups.py), so re-refreshing an overlapping
-- date range corrects prices rather than duplicating or erroring. DSE
-- does restate day-end figures occasionally, so last-write-wins on
-- (code, day) is the behaviour we want, not DO NOTHING.

-- ── market_daily_close ──────────────────────────────────────────────
-- One row per instrument per trading day. Deliberately ONLY the closing
-- price, not the full OHLC/volume row the archive offers: the tab draws
-- a close series, and storing columns nothing reads is how a schema
-- starts lying about what is maintained. The archive still has the rest
-- if a later mission needs it -- adding columns is a migration, whereas
-- un-shipping half-populated ones is not.
--
-- NOTE ON GAPS: only real trading days exist here. Weekends, Bangladesh
-- public holidays and DSE closures have NO row, and readers of this
-- table must treat an absent day as "no trading", never as zero. That
-- is the same honesty rule migration 0021's rollup_monthly_coverage
-- exists to enforce for article counts: a missing measurement and a
-- measurement of zero are different facts and must not be drawn the
-- same way. `market.monthly_closes()` therefore returns the last close
-- that actually traded in each month and OMITS a month with no trading
-- days entirely -- it does not zero-fill.
CREATE TABLE market_daily_close (
    code         TEXT NOT NULL,             -- DSE trading code, e.g. 'GP', 'ROBI'
    day          DATE NOT NULL,             -- trading day (the archive's DATE column, already YYYY-MM-DD)
    close_price  NUMERIC(14, 4) NOT NULL,   -- the archive's CLOSEP* column, in BDT
    PRIMARY KEY (code, day)
);
-- Intended query: "12 months of GP closes, one point per month" --
--   SELECT DISTINCT ON (date_trunc('month', day)) ... ORDER BY date_trunc('month', day), day DESC;
-- The primary key (code, day) already serves that scan; no extra index.

-- ── market_quote ────────────────────────────────────────────────────
-- The latest scraped snapshot of the quote page, exactly ONE row per
-- code (code is the primary key, not part of a history). This is
-- explicitly NOT a time series: intraday quotes are not something this
-- product has any use for, and keeping only the newest row means the
-- table cannot grow. The durable series lives in market_daily_close.
--
-- It exists so the tab can render the header numbers -- last price, the
-- day's move, the 52-week band, market cap -- without a live fetch, and
-- so a fetch failure degrades to "here is the last known quote, as of
-- <as_of>" instead of a blank panel. That is why `as_of` and
-- `fetched_at` are BOTH stored and are different things: `as_of` is the
-- session date dsebd.org itself printed ("Market Information: Aug 20,
-- 2026"), `fetched_at` is when we read the page. The UI needs the first
-- to label the data honestly and the second to tell whether refreshing
-- is currently working at all.
--
-- free_float_mn is NULLable alone among the numbers: DSE omits the free
-- float line for some instruments, and a quote is still worth showing
-- without it. Every other column is required -- a scrape that could not
-- find them is a parse failure and is discarded upstream rather than
-- written half-empty.
CREATE TABLE market_quote (
    code           TEXT PRIMARY KEY,          -- DSE trading code, e.g. 'GP', 'ROBI'
    name           TEXT NOT NULL,             -- company name as DSE prints it, e.g. 'Grameenphone Ltd.'
    last_price     NUMERIC(14, 4) NOT NULL,   -- Last Trading Price, BDT
    change_pct     NUMERIC(8, 4) NOT NULL,    -- Change*, percent vs. yesterday's close (may be negative)
    day_low        NUMERIC(14, 4) NOT NULL,   -- Day's Range, low side
    day_high       NUMERIC(14, 4) NOT NULL,   -- Day's Range, high side
    week52_low     NUMERIC(14, 4) NOT NULL,   -- 52 Weeks' Moving Range, low side
    week52_high    NUMERIC(14, 4) NOT NULL,   -- 52 Weeks' Moving Range, high side
    market_cap_mn  NUMERIC(18, 4) NOT NULL,   -- Market Capitalization, millions BDT
    free_float_mn  NUMERIC(18, 4),            -- Free Float Market Cap., millions BDT; NULL when DSE omits it
    as_of          DATE NOT NULL,             -- the session date DSE printed on the page, NOT our fetch date
    fetched_at     TIMESTAMPTZ NOT NULL DEFAULT now()  -- when we read the page, so staleness can be shown
);
