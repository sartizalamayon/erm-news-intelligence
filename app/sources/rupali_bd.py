"""Rupali BD — Bengali general daily. #14 by article volume. The single
news-sitemap misses sections; the sitemap INDEX's news/post/article/
latest-looking children are merged for complete coverage. Config ported
from `backend/collector_logic.py` SOURCES list (:93-96).
"""

from __future__ import annotations

from app.sources._body import fetch_generic_article_body
from app.sources._sitemap_index_merge import fetch_sitemap_index_merge
from app.sources.base import FeedItem, FetchedArticle, NewsSource
from app.sources.http import make_session
from app.sources.registry import register

INDEX_URL = "https://www.rupalibangladesh.com/sitemap.xml"


@register
class RupaliBdSource(NewsSource):
    key = "rupali_bd"
    display_name = "Rupali BD"

    def __init__(self) -> None:
        self.session = make_session(pool_size=8)

    def iter_feed_items(self) -> list[FeedItem]:
        return fetch_sitemap_index_merge(self.session, INDEX_URL, source=self.key)

    def fetch_article(self, item: FeedItem) -> FetchedArticle:
        body = fetch_generic_article_body(self.session, item.url, source=self.key)
        return FetchedArticle(item=item, body_text=body)
