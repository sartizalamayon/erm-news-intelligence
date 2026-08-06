"""Desh Rupantor — Bengali general daily. #8 by article volume.
News-sitemap. Config ported from `backend/collector_logic.py` SOURCES
list (:153-156).
"""

from __future__ import annotations

from app.sources._body import fetch_generic_article_body
from app.sources._sitemap import fetch_news_sitemap
from app.sources.base import FeedItem, FetchedArticle, NewsSource
from app.sources.http import make_session
from app.sources.registry import register

SITEMAP_URL = "https://www.deshrupantor.com/sitemaps/news-sitemap.xml"


@register
class DeshRupantorSource(NewsSource):
    key = "desh_rupantor"
    display_name = "Desh Rupantor"

    def __init__(self) -> None:
        self.session = make_session(pool_size=8)

    def iter_feed_items(self) -> list[FeedItem]:
        return fetch_news_sitemap(self.session, SITEMAP_URL, source=self.key)

    def fetch_article(self, item: FeedItem) -> FetchedArticle:
        body = fetch_generic_article_body(self.session, item.url, source=self.key)
        return FetchedArticle(item=item, body_text=body)
