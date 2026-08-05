-- 0004_article_bodies_and_seen_urls.sql
--
-- Two tables that exist purely to make the retention policy
-- (storage-policy.md §2) cheap and correct:
--
--   article_bodies — the shed-on-a-short-window half of `articles`.
--     Legacy's `content_text` (up to 4,000 chars, populated only on the
--     keyword-matched pool — legacy-behavior.md §2.7) is the single
--     heaviest field measured (ADR §4: +2,327 bytes mean, ~4,000 p95,
--     vs. ~1,127 bytes for the rest of a row). Splitting it into its own
--     1:1 table means the retention purge is a plain
--     `DELETE FROM article_bodies WHERE fetched_at < ?` — a cheap,
--     clean delete on a small table — instead of an UPDATE that leaves
--     dead tuples (and un-reclaimed TOAST storage until the next
--     autovacuum) in the wide, hot `articles` table.
--
--   seen_urls — a compact, permanent record of every url_norm ever
--     inserted into `articles`, independent of whether the full article
--     row later gets purged. This is what keeps cross-run dedup correct
--     FOREVER even after old, never-matched `articles` rows are purged
--     (storage-policy.md §2): the `articles.url_norm` UNIQUE index only
--     protects against re-inserting a URL that is still IN the table; a
--     purged row's url_norm would otherwise silently become
--     re-insertable, defeating legacy's documented "global, cross-run,
--     cross-day" dedup guarantee (legacy-behavior.md §2.6). At ~80-150
--     bytes/row (a URL string + a date) vs. ~1,127 bytes for a full
--     metadata row, remembering "this URL was already seen" costs
--     roughly 10x less than remembering the whole article.

CREATE TABLE article_bodies (
    article_id    BIGINT PRIMARY KEY REFERENCES articles (id) ON DELETE CASCADE,
    content_text  TEXT NOT NULL DEFAULT '',
    fetched_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- Intended query: the retention purge —
--   DELETE FROM article_bodies WHERE fetched_at < now() - interval '<body_retention_days> days';
-- The PK (article_id) already covers the join back to `articles` for
-- read paths (review-queue detail pane, search-archive body toggle).
CREATE INDEX idx_article_bodies_fetched_at ON article_bodies (fetched_at);

CREATE TABLE seen_urls (
    url_norm        TEXT PRIMARY KEY,
    first_seen_day  DATE NOT NULL
);
-- Intended query: the fallback half of the two-step dedup check for
-- URLs whose full `articles` row has already aged out of the raw
-- retention window —
--   SELECT 1 FROM seen_urls WHERE url_norm = ?;
-- PK lookup, O(log n) — same complexity class as the primary
-- articles.url_norm check, never a full scan, at any table size.
