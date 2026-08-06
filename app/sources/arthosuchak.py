"""Arthosuchak — Bengali finance/economy daily. #29 by article volume.
WP REST is locked (401); articles at `/archives/{id}/` with sequential
IDs. JSON-LD `datePublished` in Dhaka wall clock. Some IDs 301-redirect
to a nearby canonical (duplicates) — don't follow redirects. Config
ported from `backend/collector_logic.py` SOURCES list (:277-286).
"""

from __future__ import annotations

from app.sources._body import fetch_generic_article_body
from app.sources._id_enum import IdEnumConfig, fetch_id_enum
from app.sources.base import FeedItem, FetchedArticle, NewsSource
from app.sources.http import make_session
from app.sources.registry import register

CONFIG = IdEnumConfig(
    site_url="https://www.arthosuchak.com",
    article_pattern="/archives/{id}/",
    id_regex=r"/archives/(\d+)",
    date_tz_override="dhaka",
    not_article_check="no_time_element",
    no_follow_redirects=True,
)


@register
class ArthosuchakSource(NewsSource):
    key = "arthosuchak"
    display_name = "Arthosuchak"

    def __init__(self) -> None:
        self.session = make_session(pool_size=CONFIG.workers)

    def iter_feed_items(self) -> list[FeedItem]:
        return fetch_id_enum(self.session, CONFIG, source=self.key)

    def fetch_article(self, item: FeedItem) -> FetchedArticle:
        body = fetch_generic_article_body(self.session, item.url, source=self.key)
        return FetchedArticle(item=item, body_text=body)
