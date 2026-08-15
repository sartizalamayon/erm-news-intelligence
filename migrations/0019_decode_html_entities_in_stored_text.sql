-- 0019_decode_html_entities_in_stored_text.sql
--
-- Backfill for the clean_title() fix in app/sources/_util.py.
--
-- THE BUG
-- `clean_title` decoded entities with a hand-written map of seven:
-- &amp; &lt; &gt; &#039; &apos; &quot; &nbsp;. Every other HTML entity
-- was stored verbatim. The frontend escapes titles, correctly, because
-- they are remote input, so `&#8217;` is DISPLAYED rather than
-- rendered and the reader sees the code:
--
--   Israeli PM Netanyahu labels UK &#8216;Islamic Republic of Britain&#8217;
--   Massive oil slick hits Iran&#8217;s Qeshm Island after attack
--   পবিপ্রবিতে র&zwj;্যাগিং: ৫ সদস্যের তদন্ত কমিটি
--
-- The collector now uses html.unescape, which covers the full HTML5
-- named and numeric set, so no NEW row can arrive this way. This file
-- repairs the rows already stored.
--
-- SCOPE, measured on this database before writing the migration:
--   articles.title               220 rows
--   articles.title_en              6 rows
--   article_bodies.content_text    1 row
--   articles.llm_reason            0 rows
--   articles.topic_tag             0 rows
--
-- WHY AN EXPLICIT LIST AND NOT A GENERIC DECODER
-- Postgres has no html_unescape(), and adding a PL/Python function to a
-- free-tier Supabase instance for a one-off repair of 227 rows is a
-- worse trade than naming the twelve entities that are actually
-- present. They were counted, not guessed:
--
--   &rsquo; 115   &lsquo; 112   &#8217; 76   &#8216; 48
--   &#8211; 17    &#8212; 9     &#8221; 6    &#8230; 4
--   &zwnj; 4      &#8220; 3     &zwj; 2      &#39; 2
--
-- Anything outside that list is left alone rather than mangled by a
-- regex that guesses at codepoints.
--
-- ORDERING: `&amp;` is deliberately NOT decoded here. The old map
-- already decoded it on the way in, so no stored row still contains it,
-- and decoding it in the same pass as the rest would turn a title that
-- legitimately reads `&amp;#8217;` into a curly quote. One level of
-- decoding, once, is the whole point.
--
-- The en dash (&#8211;) and em dash (&#8212;) decode to the real
-- characters. That is correct and is not the owner's dash prohibition:
-- the rule covers copy WE write, not a publisher's own punctuation in a
-- headline we merely store.

CREATE OR REPLACE FUNCTION pg_temp.decode_common_entities(t text) RETURNS text AS $$
  SELECT replace(replace(replace(replace(replace(replace(replace(replace(
         replace(replace(replace(replace($1,
    '&rsquo;',  U&'\2019'),
    '&lsquo;',  U&'\2018'),
    '&#8217;',  U&'\2019'),
    '&#8216;',  U&'\2018'),
    '&#8211;',  U&'\2013'),
    '&#8212;',  U&'\2014'),
    '&#8221;',  U&'\201D'),
    '&#8220;',  U&'\201C'),
    '&#8230;',  U&'\2026'),
    '&zwnj;',   U&'\200C'),
    '&zwj;',    U&'\200D'),
    '&#39;',    '''')
$$ LANGUAGE sql IMMUTABLE;

UPDATE articles
   SET title = pg_temp.decode_common_entities(title)
 WHERE title ~ '&#?[a-zA-Z0-9]{2,8};';

UPDATE articles
   SET title_en = pg_temp.decode_common_entities(title_en)
 WHERE title_en IS NOT NULL
   AND title_en ~ '&#?[a-zA-Z0-9]{2,8};';

UPDATE article_bodies
   SET content_text = pg_temp.decode_common_entities(content_text)
 WHERE content_text ~ '&#?[a-zA-Z0-9]{2,8};';
