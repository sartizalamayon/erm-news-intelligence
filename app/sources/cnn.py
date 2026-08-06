"""CNN — English international news. #20 by article volume. Curated
top-stories news-sitemap (`/sitemap/news.xml`, ~88/day). Config ported
from `backend/collector_logic.py` SOURCES list (:107-109).
"""

from __future__ import annotations

from app.sources._body import fetch_generic_article_body
from app.sources._sitemap import fetch_news_sitemap
from app.sources.base import FeedItem, FetchedArticle, NewsSource
from app.sources.http import make_session
from app.sources.registry import register

SITEMAP_URL = "https://www.cnn.com/sitemap/news.xml"


@register
class CnnSource(NewsSource):
    key = "cnn"
    display_name = "CNN"

    def __init__(self) -> None:
        self.session = make_session(pool_size=8)

    def iter_feed_items(self) -> list[FeedItem]:
        return fetch_news_sitemap(self.session, SITEMAP_URL, source=self.key)

    def fetch_article(self, item: FeedItem) -> FetchedArticle:
        body = fetch_generic_article_body(self.session, item.url, source=self.key)
        return FetchedArticle(item=item, body_text=body)
