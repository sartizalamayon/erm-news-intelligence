"""Ekattor TV — Bengali general daily. #32 by article volume. Plain RSS
feed. Config ported from `backend/collector_logic.py` SOURCES list
(:202-204).
"""

from __future__ import annotations

from app.sources._body import fetch_generic_article_body
from app.sources._rss import fetch_rss
from app.sources.base import FeedItem, FetchedArticle, NewsSource
from app.sources.http import make_session
from app.sources.registry import register

FEED_URL = "https://ekattor.tv/feed/"


@register
class EkattorTvSource(NewsSource):
    key = "ekattor_tv"
    display_name = "Ekattor TV"

    def __init__(self) -> None:
        self.session = make_session(pool_size=4)

    def iter_feed_items(self) -> list[FeedItem]:
        return fetch_rss(self.session, FEED_URL, source=self.key)

    def fetch_article(self, item: FeedItem) -> FetchedArticle:
        body = fetch_generic_article_body(self.session, item.url, source=self.key)
        return FetchedArticle(item=item, body_text=body)
