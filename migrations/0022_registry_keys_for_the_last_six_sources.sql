-- 0022_registry_keys_for_the_last_six_sources.sql
--
-- The last six sources that could never fetch.
--
-- THE STATE THIS FIXES
-- Amarbarta, BSS, Deshpatrika, InfoTech, ShareBiz and ShareBusiness24
-- were seeded in `sources` with a `method` name but no plugin behind
-- it, so `registry_key` stayed NULL. Collection iterates the CODE
-- registry (app/services/collector.py::enabled_source_classes) and uses
-- this table only to VETO, so a NULL registry_key means the source is
-- skipped by every run. All six sat enabled, reporting "no run data
-- yet" (the quiet-day wording) while being structurally incapable of
-- returning an article.
--
-- Plugins now exist for all six (app/sources/{amar_barta,bss,
-- deshpatrika,infotech_insight,share_biz,share_business_24}.py) and are
-- listed in registry._SOURCE_MODULES. This migration is the last link:
-- it points each row at its plugin so the veto correlation works and
-- the health screen stops flagging them.
--
-- WHAT EACH SOURCE ACTUALLY SUPPORTS, measured live 2026-08-17.
-- Recorded here because the next person to look at these rows will ask
-- why five of six use Google News, and the answer is not laziness:
--
--   Amarbarta        NATIVE archive endpoint. Returns its own listing
--                    with real Bengali-digit timestamps
--                    (2026-08-17 14:12:15). 20 items per page, dated
--                    and titled. gnews kept only as a fallback.
--
--   BSS              No machine-readable date ANYWHERE. /sitemap-news.xml
--                    (the URL it was configured with) is a soft 404;
--                    /sitemap.xml is real but has no <lastmod>; article
--                    pages carry no JSON-LD, no article:published_time
--                    and no <time>. This pipeline files by PUBLICATION
--                    date, so an undated superset is worth less than a
--                    dated subset. gnews: 200 dated items.
--
--   Deshpatrika      Same: no JSON-LD, no og:published_time, no <time>.
--                    The one ISO date in the page is the archive form's
--                    default (today), which would stamp every article
--                    with its crawl date -- the exact bug 0018 undoes.
--                    gnews: 4 dated items.
--
--   InfoTech         DOES expose JSON-LD datePublished, so _id_enum's
--                    existing jsonld variant could walk it natively for
--                    more volume. Left as gnews (7 dated items) because
--                    an id-walk is one request per article and this
--                    source is a fraction of a percent of the corpus.
--                    Noted in its docstring as the upgrade path.
--
--   ShareBiz         Archive endpoint exists but rejects the visible
--   ShareBusiness24  form's parameters with "Undefined array key
--                    action". Reverse-engineering an undocumented
--                    private parameter is not worth it while gnews
--                    returns 100 and 33 dated items respectively.
--                    _archive_listing is already built for this exact
--                    endpoint shape if that changes.
--
-- Idempotent by the migration runner's once-per-environment guarantee,
-- and defensively scoped to rows that are still NULL so re-running this
-- statement by hand cannot overwrite a key someone has since corrected.

UPDATE sources SET registry_key = 'amar_barta'        WHERE name = 'Amarbarta'       AND registry_key IS NULL;
UPDATE sources SET registry_key = 'bss'               WHERE name = 'BSS'             AND registry_key IS NULL;
UPDATE sources SET registry_key = 'deshpatrika'       WHERE name = 'Deshpatrika'     AND registry_key IS NULL;
UPDATE sources SET registry_key = 'infotech_insight'  WHERE name = 'InfoTech'        AND registry_key IS NULL;
UPDATE sources SET registry_key = 'share_biz'         WHERE name = 'ShareBiz'        AND registry_key IS NULL;
UPDATE sources SET registry_key = 'share_business_24' WHERE name = 'ShareBusiness24' AND registry_key IS NULL;
