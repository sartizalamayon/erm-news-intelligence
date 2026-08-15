-- 0020_decode_mdash_in_body_text.sql
--
-- Finishes 0019. That migration's entity list was counted from
-- `articles.title`, which is where 220 of the 227 affected rows were,
-- and `&mdash;` occurs only in `article_bodies.content_text` -- so it
-- was not in the list and the row was left as-is. That is the
-- documented behaviour of 0019 ("anything outside that list is left
-- alone rather than mangled"), not a failure of it, but leaving one
-- known row half-decoded is not worth preserving.
--
-- Re-measured across EVERY text column after 0019 ran
-- (articles.title, title_en, llm_reason, topic_tag and
-- article_bodies.content_text): `&mdash;` x1 is all that remains.
--
-- The row is an Al Jazeera story whose extracted body kept the markup
-- of an embedded tweet. Only 2 of 9,171 bodies contain stray tags at
-- all, so that extraction edge case is noted and left alone rather than
-- chased: bodies feed keyword matching and LLM prompts, where a stray
-- anchor is noise, not a defect.
--
-- The em dash this decodes to is a publisher's own punctuation inside a
-- quoted tweet, not copy this project writes, so the prohibition on em
-- dashes in UI text does not apply (same reasoning as 0019's note on
-- &#8211; / &#8212;).

UPDATE article_bodies
   SET content_text = replace(content_text, '&mdash;', U&'\2014')
 WHERE content_text LIKE '%&mdash;%';
