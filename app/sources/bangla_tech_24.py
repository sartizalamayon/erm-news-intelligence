"""Bangla Tech 24 — Bengali tech daily. #50 by article volume (smallest
ported source — 2 historical articles; included because it's "free"
once the wp_rest helper exists for a same-method neighbor). WordPress
REST API is open. Config ported from `backend/collector_logic.py`
SOURCES list (:329-330).
"""

from __future__ import annotations

from app.sources._body import fetch_generic_article_body
from app.sources._wp_rest import fetch_wp_rest
from app.sources.base import FeedItem, FetchedArticle, NewsSource
from app.sources.http import make_session
from app.sources.registry import register

SITE_URL = "https://banglatech24.com"


@register
class BanglaTech24Source(NewsSource):
    key = "bangla_tech_24"
    display_name = "Bangla Tech 24"

    def __init__(self) -> None:
        self.session = make_session(pool_size=8)

    def iter_feed_items(self) -> list[FeedItem]:
        return fetch_wp_rest(self.session, SITE_URL, source=self.key)

    def fetch_article(self, item: FeedItem) -> FetchedArticle:
        body = fetch_generic_article_body(self.session, item.url, source=self.key)
        return FetchedArticle(item=item, body_text=body)
