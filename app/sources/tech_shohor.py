"""Tech Shohor — Bengali tech daily. #49 by article volume. Google News
search is the sole authoritative method. Config ported from
`backend/collector_logic.py` SOURCES list (:349).
"""

from __future__ import annotations

from app.sources._body import fetch_generic_article_body
from app.sources._gnews import fetch_gnews
from app.sources.base import FeedItem, FetchedArticle, NewsSource
from app.sources.http import make_session
from app.sources.registry import register

DOMAIN = "techshohor.com"


@register
class TechShohorSource(NewsSource):
    key = "tech_shohor"
    display_name = "Tech Shohor"

    def __init__(self) -> None:
        self.session = make_session(pool_size=4)

    def iter_feed_items(self) -> list[FeedItem]:
        return fetch_gnews(self.session, DOMAIN, source=self.key)

    def fetch_article(self, item: FeedItem) -> FetchedArticle:
        body = fetch_generic_article_body(self.session, item.url, source=self.key)
        return FetchedArticle(item=item, body_text=body)
