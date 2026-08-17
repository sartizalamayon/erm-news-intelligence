"""Daily ICT News — Bengali tech/ICT daily. #47 by article volume
(small, ported "for free" once `_id_enum`'s `bengali_date` parser
existed for Somoy Journal). Custom CMS, articles at bare `/{id}`. Date
is Bengali text ("● ১৮ জুন ২০২৬") — no JSON-LD; non-articles have no
Bengali date, so the date-parse failure itself rejects them. Config
ported from `backend/collector_logic.py` SOURCES list (:334-340).
"""

from __future__ import annotations

from app.sources._body import fetch_generic_article_body
from app.sources._id_enum import IdEnumConfig, fetch_id_enum
from app.sources.base import FeedItem, FetchedArticle, NewsSource
from app.sources.http import make_session
from app.sources.registry import register

CONFIG = IdEnumConfig(
    site_url="https://www.dailyictnews.com",
    article_pattern="/{id}",
    # The trailing lookahead is load-bearing. Without it the homepage's
    # image filenames match: `/content/archives/2026/08/9007442b-f8d6-...`
    # yielded a "9007442" id against a real newest article id of 17353,
    # and the walk spent its whole probe budget 8.99 million ids above
    # the top of the site (run 125: "0 fetched" after 56.7s).
    # `_id_enum._plausible_max_id` now also rejects that id as an
    # outlier; this keeps the regex from producing it in the first place.
    id_regex=r"(?:dailyictnews\.com)?/(\d{4,})(?![\dA-Za-z])",
    date_source="bengali_date",
    not_article_check="none",
)


@register
class DailyIctNewsSource(NewsSource):
    key = "daily_ict_news"
    display_name = "Daily ICT News"

    def __init__(self) -> None:
        self.session = make_session(pool_size=CONFIG.workers)

    def iter_feed_items(self) -> list[FeedItem]:
        return fetch_id_enum(self.session, CONFIG, source=self.key)

    def fetch_article(self, item: FeedItem) -> FetchedArticle:
        body = fetch_generic_article_body(self.session, item.url, source=self.key)
        return FetchedArticle(item=item, body_text=body)
