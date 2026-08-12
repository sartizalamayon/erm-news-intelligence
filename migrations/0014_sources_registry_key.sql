-- 0014_sources_registry_key.sql
--
-- Bug fix (P1-INGEST/ADMIN-API reconciliation audit): `sources.name` is
-- admin-editable (PATCH /sources/{id}) but test/diagnose
-- (app.services.admin.probe_source / diagnose_source) and the new
-- enabled-filtering in app.services.collector.run_collection both need
-- a STABLE identifier for the code-side plugin registry
-- (app.sources.registry's per-NewsSource-subclass `key`) that survives
-- a rename. `name` was never that -- matching by display name (the
-- convention every `app/sources/*.py` module already follows) breaks
-- the instant an admin renames a source, because the lookup re-matches
-- on the NEW name, which no longer equals any registry key's
-- `display_name`.
--
-- registry_key is nullable and intentionally never made NOT NULL: 6 of
-- the 51 seeded `sources` rows (BSS, ShareBiz, ShareBusiness24,
-- InfoTech, Deshpatrika, Amarbarta) have no corresponding entry in
-- app/sources/registry.py -- the documented "45 of 51 legacy sources
-- ported" gap (that module's own docstring). There is no plugin class
-- to correlate those 6 rows to, so they keep registry_key = NULL;
-- test/diagnose (app.services.admin) and the enabled-filter
-- (app.services.collector) both already treat "no registry_key" as "no
-- fetch plugin" / "no DB opinion on this registry key", never a crash
-- or a silent full-registry disable.
--
-- Backfill verified against the real dev DB (erm_dev) before this
-- migration shipped: every one of the OTHER 45 rows' `name` matched
-- exactly one registry `display_name`, 1:1, with zero ambiguity and
-- zero leftover unmatched rows on the registry side (every one of the
-- 45 `app.sources.registry.all_sources()` keys resolved to exactly one
-- `sources` row) -- see the per-pair UPDATE statements below, generated
-- directly from the live registry, one per matched (name, key) pair.
ALTER TABLE sources ADD COLUMN registry_key TEXT;

UPDATE sources SET registry_key = 'ajker_patrika' WHERE name = 'Ajker Patrika';
UPDATE sources SET registry_key = 'al_jazeera' WHERE name = 'Al Jazeera';
UPDATE sources SET registry_key = 'arthosuchak' WHERE name = 'Arthosuchak';
UPDATE sources SET registry_key = 'bangla_tech_24' WHERE name = 'Bangla Tech 24';
UPDATE sources SET registry_key = 'bangladesh_post' WHERE name = 'Bangladesh Post';
UPDATE sources SET registry_key = 'bbc_news' WHERE name = 'BBC News';
UPDATE sources SET registry_key = 'bd_news_24' WHERE name = 'BD News 24';
UPDATE sources SET registry_key = 'bd_pratidin' WHERE name = 'BD Pratidin';
UPDATE sources SET registry_key = 'bonik_barta' WHERE name = 'Bonik Barta';
UPDATE sources SET registry_key = 'business_standard' WHERE name = 'The Business Standard';
UPDATE sources SET registry_key = 'channel_i_online' WHERE name = 'Channel i Online';
UPDATE sources SET registry_key = 'channels' WHERE name = 'Channels';
UPDATE sources SET registry_key = 'cnn' WHERE name = 'CNN';
UPDATE sources SET registry_key = 'daily_adin' WHERE name = 'Daily Adin';
UPDATE sources SET registry_key = 'daily_ict_news' WHERE name = 'Daily ICT News';
UPDATE sources SET registry_key = 'daily_inqilab' WHERE name = 'Daily Inqilab';
UPDATE sources SET registry_key = 'daily_sangram' WHERE name = 'Daily Sangram';
UPDATE sources SET registry_key = 'daily_star' WHERE name = 'Daily Star';
UPDATE sources SET registry_key = 'daily_sun' WHERE name = 'Daily Sun';
UPDATE sources SET registry_key = 'desh_protikhon' WHERE name = 'Desh Protikhon';
UPDATE sources SET registry_key = 'desh_rupantor' WHERE name = 'Desh Rupantor';
UPDATE sources SET registry_key = 'deshersangbad' WHERE name = 'Deshersangbad';
UPDATE sources SET registry_key = 'dhaka_mail' WHERE name = 'Dhaka Mail';
UPDATE sources SET registry_key = 'dhaka_post' WHERE name = 'Dhaka Post';
UPDATE sources SET registry_key = 'dhaka_tribune' WHERE name = 'Dhaka Tribune';
UPDATE sources SET registry_key = 'digibangla' WHERE name = 'DigiBangla';
UPDATE sources SET registry_key = 'economy_post' WHERE name = 'Economy Post';
UPDATE sources SET registry_key = 'ekattor_tv' WHERE name = 'Ekattor TV';
UPDATE sources SET registry_key = 'financial_express' WHERE name = 'The Financial Express';
UPDATE sources SET registry_key = 'independent_tv' WHERE name = 'Independent TV';
UPDATE sources SET registry_key = 'kalbela' WHERE name = 'Kalbela';
UPDATE sources SET registry_key = 'nagorik' WHERE name = 'Nagorik';
UPDATE sources SET registry_key = 'naya_diganta' WHERE name = 'Naya Diganta';
UPDATE sources SET registry_key = 'ndtv' WHERE name = 'NDTV';
UPDATE sources SET registry_key = 'new_age' WHERE name = 'New Age';
UPDATE sources SET registry_key = 'news24_bd' WHERE name = 'News24 BD';
UPDATE sources SET registry_key = 'prothom_alo' WHERE name = 'Prothom Alo';
UPDATE sources SET registry_key = 'prothom_alo_english' WHERE name = 'Prothom Alo English';
UPDATE sources SET registry_key = 'rupali_bd' WHERE name = 'Rupali BD';
UPDATE sources SET registry_key = 'sangbad' WHERE name = 'Sangbad';
UPDATE sources SET registry_key = 'shomoyer_alo' WHERE name = 'Shomoyer Alo';
UPDATE sources SET registry_key = 'somoy_journal' WHERE name = 'Somoy Journal';
UPDATE sources SET registry_key = 'tech_shohor' WHERE name = 'Tech Shohor';
UPDATE sources SET registry_key = 'techworld' WHERE name = 'TechWorld';
UPDATE sources SET registry_key = 'times_of_bangladesh' WHERE name = 'Times of Bangladesh';

-- One registry key per row going forward -- catches a future double
-- backfill or a bad manual UPDATE before it silently makes test/probe
-- resolve to the wrong plugin. NULL rows (the 6 unmatched legacy
-- sources) are deliberately excluded from this constraint.
CREATE UNIQUE INDEX idx_sources_registry_key ON sources (registry_key) WHERE registry_key IS NOT NULL;
