-- 0018_articles_day_is_publication_date.sql
--
-- Re-files every existing article under the date it was PUBLISHED
-- rather than the date a collection run happened to pick it up.
--
-- WHY
-- ---
-- 0003_articles.sql defines `day` as "the COLLECTION day (run start),
-- not necessarily the article's published day — same semantics as
-- legacy". That is an operational fact wearing an editorial label, and
-- it is wrong for most of the corpus. The fetch window is a rolling 30
-- hours (app/sources/_util.py's WINDOW), so every run legitimately
-- collects the previous evening's news — and filed all of it under the
-- run's own date.
--
-- Measured across 60,058 articles immediately before this migration:
--
--     day == published date        25,845
--     day is 1 day LATE            30,653
--     day is 2 days LATE            2,251
--     day is 3-6 days LATE            ~700
--     -----------------------------------
--     34,130 rows (56.8%) filed after the day they actually happened
--
-- So "14 August" in the review queue meant "whatever the 14 August run
-- collected", which was mostly 13 August news. Every day-scoped screen
-- in the product — review queue, dashboard, search, rollups — inherited
-- that skew.
--
-- WHAT
-- ----
-- `day` becomes the publication date in DHAKA local time. Dhaka because
-- the desk works in Dhaka: a story published 02:00 Dhaka is that day's
-- news to them even though UTC still calls it yesterday. `published` is
-- `timestamptz`, so `AT TIME ZONE 'Asia/Dhaka'` converts correctly from
-- whatever offset each source supplied.
--
-- Rows with `published IS NULL` keep their collection day — it is the
-- only honest guess available. That is 61 rows (0.10%), all from a
-- single incident on 2026-07-18; a real news article essentially always
-- carries a date.
--
-- ACCEPTED TRADE-OFF (owner's explicit decision)
-- ----------------------------------------------
-- An article published at 23:00 and collected by a 01:00 run now lands
-- on the PREVIOUS day, which the analyst may already have reviewed, and
-- could therefore be missed. Correct dates were chosen over guaranteed
-- visibility. `review_status` is NOT touched here, so anything already
-- approved or rejected keeps its decision — only the date it is filed
-- under moves.

-- FUTURE DATES ARE MOSTLY LEGITIMATE, so this does not clamp them.
-- A newspaper publishes tomorrow's edition tonight, and 1,222 real rows
-- do exactly that — always exactly +1 day, across BD Pratidin, Daily
-- Star, Daily Inqilab, Naya Diganta and others:
--
--     BD Pratidin    published 2026-08-01 06:00  fetched 2026-07-31 20:35
--     Daily Star     published 2026-08-01 12:00  fetched 2026-07-31 20:37
--
-- The edition date is the date a reader means, so those keep it. Only
-- IMPLAUSIBLE futures are pulled back to the collection day: 2 Somoy
-- Journal event-announcement pages whose parser picked up the date of
-- the EVENT (41 and 86 days out) instead of the publication date. The
-- 2-day threshold is `app.sources._util.FUTURE_TOLERANCE`.
UPDATE articles
   SET day = (published AT TIME ZONE 'Asia/Dhaka')::date
 WHERE published IS NOT NULL
   AND day <> (published AT TIME ZONE 'Asia/Dhaka')::date
   AND (published AT TIME ZONE 'Asia/Dhaka')::date - (fetched_at AT TIME ZONE 'Asia/Dhaka')::date <= 2;

UPDATE articles
   SET day = (fetched_at AT TIME ZONE 'Asia/Dhaka')::date
 WHERE published IS NOT NULL
   AND (published AT TIME ZONE 'Asia/Dhaka')::date - (fetched_at AT TIME ZONE 'Asia/Dhaka')::date > 2;
