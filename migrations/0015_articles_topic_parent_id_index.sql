-- 0015_articles_topic_parent_id_index.sql
--
-- `articles.topic_parent_id` self-references `articles.id`
-- (`ON DELETE SET NULL`, see 0003_articles.sql) with no supporting index.
-- Discovered live: deleting ~6,600 rows from `articles` (a routine
-- test-data cleanup) hung for 80+ seconds. Cause: for every row deleted,
-- Postgres must check whether any OTHER row's `topic_parent_id` points
-- at it, so it can SET NULL there — with no index on that column, each
-- check is a full sequential scan of the whole table. This is the same
-- class of bug as the original P1 dedup lookup (speed-techniques.md
-- §1) that motivated this project's rebuild, just recurring on a write
-- path (delete) rather than a read path, in a column nobody had reason
-- to query directly until now.

CREATE INDEX idx_articles_topic_parent_id
    ON articles (topic_parent_id)
    WHERE topic_parent_id IS NOT NULL;
