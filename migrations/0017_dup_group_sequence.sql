-- 0017_dup_group_sequence.sql
--
-- Makes `articles.dup_group` a GLOBALLY unique cluster id.
--
-- 0003's column comment describes dup_group as "cluster id, locally
-- scoped per run per legacy semantics -- NOT a global identity". That
-- was a faithful port of legacy's behaviour, and it is unsafe here,
-- because this schema's readers do not scope by run or by day:
--
--     app/api/routers/review.py:332
--         SELECT ... FROM articles WHERE dup_group = ANY(%s)
--     app/api/routers/review.py:570
--         SELECT id, source FROM articles WHERE dup_group = %s
--
-- With per-run numbering restarting at 1, "group 1" from today and
-- "group 1" from any previous day are the same value, so those queries
-- would return articles from unrelated days as duplicate siblings of
-- today's story — silently, and in the reviewer-facing UI.
--
-- The bug was dormant only because the dedup stage never actually
-- assigned a group: its embedding backend has always been
-- `UnavailableEmbeddingBackend`, so `dup_group` stayed NULL on every
-- row ever written (see app/services/near_duplicates.py's header).
-- Implementing real clustering is what would have activated it, so the
-- id space is corrected first, in the same change.
--
-- A sequence, rather than "max(dup_group) + 1" computed in Python:
-- allocation stays correct if two runs ever overlap (the collection
-- background thread and an admin's manual stage re-run are genuinely
-- concurrent today), and it never needs to read the table.

CREATE SEQUENCE IF NOT EXISTS articles_dup_group_seq AS BIGINT START WITH 1;

-- Any dup_group values already written under the old per-run numbering
-- are ambiguous by construction — there is no way to tell which run a
-- given "1" belonged to. There are none in practice (the stage was a
-- no-op), but clearing them is the only correct handling of a value
-- whose meaning cannot be recovered, and leaving them would let the
-- exact cross-day mixing described above happen for those rows.
UPDATE articles
   SET dup_group = NULL, is_primary = TRUE, also_in = ''
 WHERE dup_group IS NOT NULL;
