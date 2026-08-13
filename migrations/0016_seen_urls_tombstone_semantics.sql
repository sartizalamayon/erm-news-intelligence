-- 0016_seen_urls_tombstone_semantics.sql
--
-- Fixes a live, user-visible correctness bug: re-fetching a day after
-- deleting its articles silently returned "0 new" for almost every
-- source, making the whole Run-collection feature untestable and
-- un-rerunnable.
--
-- ROOT CAUSE
-- ----------
-- `seen_urls` was written on EVERY article INSERT
-- (app/services/collector.py's `remember_seen_url()` call), while
-- `articles` rows can be deleted by paths that never touch `seen_urls`
-- (the manual admin release valve `purge_matched_articles`, and any
-- ad-hoc/operational DELETE). The two tables therefore drift: measured
-- live on 2026-08-14, `seen_urls` held 8,931 url_norms whose `articles`
-- row no longer existed. Every one of those URLs was permanently
-- un-refetchable, because `write_items()` consults `seen_urls` BEFORE
-- attempting the insert. Symptom in the UI: "Sangbad — 178 fetched · 4
-- new" on a day whose articles had just been deleted.
--
-- WHY THE OLD SHAPE WAS WRONG (not just unlucky)
-- ----------------------------------------------
-- 0004's own header justifies `seen_urls` as the thing that keeps
-- cross-run dedup correct "even after old, never-matched `articles`
-- rows are purged" — i.e. its job is to be a TOMBSTONE for rows that no
-- longer exist. Writing it on insert instead made it a duplicate of the
-- live `articles.url_norm` index (1 seen_urls row per live article —
-- 10,691 rows for 10,691 articles, zero of the promised ~10x saving),
-- and coupled correctness to every caller remembering to clean up.
-- "Every delete path must remember to maintain this table" is not an
-- invariant a codebase can hold for long; the live orphan count proves
-- it already failed.
--
-- THE FIX
-- -------
-- Maintain `seen_urls` from the one event that actually defines its
-- meaning — an `articles` row ceasing to exist — using a trigger, so no
-- delete path (service code, admin action, migration, or a human at
-- psql) can bypass it. The dedup guarantee is unchanged and strictly
-- better enforced:
--
--   * URL still live in `articles`  -> blocked by idx_articles_url_norm
--                                      (UNIQUE), as before.
--   * URL deleted/purged from       -> blocked by seen_urls, now
--     `articles`                       populated automatically.
--
-- A statement-level trigger with a transition table (not FOR EACH ROW)
-- so a bulk purge of N rows costs one INSERT ... SELECT, not N trigger
-- invocations — this table is deleted from in large batches by the
-- retention purge, which is exactly the case a row-level trigger would
-- make pathological.
--
-- Deliberately re-running a day (delete + re-collect) is a REAL
-- workflow, and it is now served explicitly by
-- `app.services.admin.reset_day()`, which drops the tombstones it just
-- created in the same transaction. That is the supported way to make a
-- day re-fetchable — as opposed to the old behaviour, where an ad-hoc
-- DELETE appeared to work and then silently poisoned every later run.

CREATE OR REPLACE FUNCTION remember_deleted_url_norms() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO seen_urls (url_norm, first_seen_day)
    SELECT url_norm, day FROM deleted_rows WHERE url_norm <> ''
    ON CONFLICT (url_norm) DO NOTHING;
    RETURN NULL;
END;
$$;

DROP TRIGGER IF EXISTS articles_remember_deleted_urls ON articles;
CREATE TRIGGER articles_remember_deleted_urls
    AFTER DELETE ON articles
    REFERENCING OLD TABLE AS deleted_rows
    FOR EACH STATEMENT
    EXECUTE FUNCTION remember_deleted_url_norms();

-- Data repair, part 1 — provably redundant rows.
-- A url_norm whose article is STILL LIVE is already blocked by the
-- UNIQUE index idx_articles_url_norm; its seen_urls row can never be
-- the reason an insert is rejected. Dropping these is behaviour-
-- preserving by construction, and is what shrinks the table back to the
-- tombstones-only shape 0004 intended. The trigger above re-adds each
-- one at the moment its article is actually deleted.
DELETE FROM seen_urls s
WHERE EXISTS (SELECT 1 FROM articles a WHERE a.url_norm = s.url_norm);
