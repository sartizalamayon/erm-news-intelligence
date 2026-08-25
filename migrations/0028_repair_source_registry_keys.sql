-- 0028_repair_source_registry_keys.sql
--
-- The backfill that ran before there was anything to backfill.
--
-- WHAT WENT WRONG
-- `sources.registry_key` (0014) is the stable correlation between a
-- `sources` row and the code-side fetch plugin
-- (app/sources/registry.py's per-NewsSource `key`). Three things
-- resolve through it: the enable/disable veto in
-- app/services/collector.py, the Sources screen's per-source
-- test/diagnose (app.services.admin._plugin_for_registry_key), and the
-- dashboard's "sources ready" denominator.
--
-- 0014 backfilled 45 pairs and 0022 the last 6, and both are pure
-- `UPDATE sources SET registry_key = ... WHERE name = ...`. Neither
-- inserts a row, and neither does any other migration -- grep the whole
-- chain: `INSERT INTO sources` appears nowhere in migrations/*.sql. The
-- only two writers in the repo are the admin "add a source" form
-- (app.services.admin.create_source, which sets no registry_key) and
-- migrations/legacy/import_legacy_sqlite.py, which is deliberately NOT
-- part of this chain -- it is a one-off script, run by hand, and its
-- own usage note requires the chain to have been applied FIRST.
--
-- So the real order on a fresh deploy is:
--
--     1. python -m app.db.migrate   -> 0014 and 0022 UPDATE an EMPTY
--                                      table, change 0 rows, and are
--                                      recorded applied + checksummed.
--     2. import_legacy_sqlite.py    -> 51 rows land, every one with
--                                      registry_key = NULL.
--     3. (nothing)                     0014/0022 can never run again;
--                                      app/db/migrate.py refuses to
--                                      re-run a recorded file at all,
--                                      and would refuse even harder if
--                                      the file were edited.
--
-- The result is permanent: the toggle on the Sources screen writes
-- `enabled = FALSE` to a row collection cannot correlate to a plugin,
-- so the source keeps being fetched and the switch appears to do
-- nothing; every per-source diagnostic reports "no plugin"; and the
-- dashboard's fetched-today tile read "0 of 0 sources responded" on a
-- day 45 plugins had run.
--
-- THIS MIGRATION IS HALF OF THE FIX, AND ONLY HALF. SAID PLAINLY.
-- A migration runs once, at whatever moment the deploy reaches it, and
-- it cannot repair rows that do not exist yet. That is exactly how
-- 0014 and 0022 became no-ops, and nothing about writing a third one
-- changes that property. What this file is for is the databases where
-- the rows are ALREADY sitting there unkeyed -- every environment that
-- has run the legacy import to date -- so they are repaired at deploy
-- time rather than at the next collection run.
--
-- The other half, the half that is order-independent, is
-- `app.services.collector.reconcile_source_registry_keys`, called by
-- `run_collection` on every run, scheduled or manual. It derives the
-- same name -> key correlation from `app.sources.registry` itself and
-- fills any NULL it finds, so rows imported at ANY point after this
-- migration are keyed the next time collection happens, with no
-- operator step to remember and no fourth migration. If you are reading
-- this because the keys are NULL again, that function -- not another
-- copy of this file -- is where to look.
--
-- HOW THE PAIRS BELOW WERE PRODUCED
-- Generated directly from the live registry
-- (`NewsSource.display_name` -> `NewsSource.key` for all 51 registered
-- plugins), the same way 0014's own header records its list being
-- generated, and the same values `reconcile_source_registry_keys`
-- computes at runtime. `display_name` is what the collector writes into
-- `articles.source`, so it is the field a legacy `sources.name` was
-- always meant to line up with.
--
-- SAFETY, since name matching is a blunt instrument
--   * `registry_key IS NULL` only. A key set by an admin, by 0014/0022
--     on an older database, or by the runtime reconcile is never
--     overwritten.
--   * `NOT EXISTS (... t.registry_key = p.registry_key)` -- a key
--     already held by another row is left alone. `idx_sources_registry_key`
--     is UNIQUE; without this guard one duplicate name could fail the
--     whole migration and block the deploy.
--   * `HAVING count(*) = 1` -- if two unkeyed rows ever mapped to one
--     key, both are skipped rather than one being picked. `sources.name`
--     is UNIQUE (0002), so on this schema that cannot happen and this
--     clause is unreachable; it costs one line and it is what stands
--     between a future schema change and a source silently keyed to the
--     wrong plugin.
--
-- Idempotent and safe to re-run by hand: after it has run, every row it
-- would touch is non-NULL, so the candidate set is empty.

WITH plugin(name, registry_key) AS (
  VALUES
    ('Ajker Patrika'        , 'ajker_patrika'),
    ('Al Jazeera'           , 'al_jazeera'),
    ('Amarbarta'            , 'amar_barta'),
    ('Arthosuchak'          , 'arthosuchak'),
    ('BBC News'             , 'bbc_news'),
    ('BD News 24'           , 'bd_news_24'),
    ('BD Pratidin'          , 'bd_pratidin'),
    ('BSS'                  , 'bss'),
    ('Bangla Tech 24'       , 'bangla_tech_24'),
    ('Bangladesh Post'      , 'bangladesh_post'),
    ('Bonik Barta'          , 'bonik_barta'),
    ('CNN'                  , 'cnn'),
    ('Channel i Online'     , 'channel_i_online'),
    ('Channels'             , 'channels'),
    ('Daily Adin'           , 'daily_adin'),
    ('Daily ICT News'       , 'daily_ict_news'),
    ('Daily Inqilab'        , 'daily_inqilab'),
    ('Daily Sangram'        , 'daily_sangram'),
    ('Daily Star'           , 'daily_star'),
    ('Daily Sun'            , 'daily_sun'),
    ('Desh Protikhon'       , 'desh_protikhon'),
    ('Desh Rupantor'        , 'desh_rupantor'),
    ('Deshersangbad'        , 'deshersangbad'),
    ('Deshpatrika'          , 'deshpatrika'),
    ('Dhaka Mail'           , 'dhaka_mail'),
    ('Dhaka Post'           , 'dhaka_post'),
    ('Dhaka Tribune'        , 'dhaka_tribune'),
    ('DigiBangla'           , 'digibangla'),
    ('Economy Post'         , 'economy_post'),
    ('Ekattor TV'           , 'ekattor_tv'),
    ('Independent TV'       , 'independent_tv'),
    ('InfoTech'             , 'infotech_insight'),
    ('Kalbela'              , 'kalbela'),
    ('NDTV'                 , 'ndtv'),
    ('Nagorik'              , 'nagorik'),
    ('Naya Diganta'         , 'naya_diganta'),
    ('New Age'              , 'new_age'),
    ('News24 BD'            , 'news24_bd'),
    ('Prothom Alo'          , 'prothom_alo'),
    ('Prothom Alo English'  , 'prothom_alo_english'),
    ('Rupali BD'            , 'rupali_bd'),
    ('Sangbad'              , 'sangbad'),
    ('ShareBiz'             , 'share_biz'),
    ('ShareBusiness24'      , 'share_business_24'),
    ('Shomoyer Alo'         , 'shomoyer_alo'),
    ('Somoy Journal'        , 'somoy_journal'),
    ('Tech Shohor'          , 'tech_shohor'),
    ('TechWorld'            , 'techworld'),
    ('The Business Standard', 'business_standard'),
    ('The Financial Express', 'financial_express'),
    ('Times of Bangladesh'  , 'times_of_bangladesh')
),
candidate AS (
  SELECT s.id, p.registry_key
    FROM sources s
    JOIN plugin p ON p.name = s.name
   WHERE s.registry_key IS NULL
     AND NOT EXISTS (SELECT 1 FROM sources t WHERE t.registry_key = p.registry_key)
),
unambiguous AS (
  SELECT registry_key, min(id) AS id
    FROM candidate
   GROUP BY registry_key
  HAVING count(*) = 1
)
UPDATE sources s
   SET registry_key = u.registry_key
  FROM unambiguous u
 WHERE s.id = u.id;
