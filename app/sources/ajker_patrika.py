"""Ajker Patrika — Bengali general daily. #33 by article volume.
News-sitemap, with a genuine empty-result RSS fallback (speed-
techniques.md §4 point 4). Config ported from
`backend/collector_logic.py` SOURCES list (:158-161).
"""

from __future__ import annotations

from app.logging import get_logger
from app.sources._body import fetch_generic_article_body
from app.sources._rss import fetch_rss
from app.sources._sitemap import fetch_news_sitemap
from app.sources.base import FeedItem, FetchedArticle, NewsSource
from app.sources.http import make_session
from app.sources.registry import register

logger = get_logger(__name__)

SITEMAP_URL = "https://www.ajkerpatrika.com/sitemaps/news-sitemap.xml"
FALLBACK_RSS_URL = "https://www.ajkerpatrika.com/feed"


@register
class AjkerPatrikaSource(NewsSource):
    key = "ajker_patrika"
    display_name = "Ajker Patrika"

    def __init__(self) -> None:
        self.session = make_session(pool_size=8)

    def iter_feed_items(self) -> list[FeedItem]:
        items = fetch_news_sitemap(self.session, SITEMAP_URL, source=self.key)
        if items:
            return items
        logger.warning(
            "source_fallback_triggered",
            extra={"source": self.key, "primary_method": "sitemap", "fallback_method": "rss"},
        )
        return fetch_rss(self.session, FALLBACK_RSS_URL, source=self.key, required=False)

    def fetch_article(self, item: FeedItem) -> FetchedArticle:
        body = fetch_generic_article_body(self.session, item.url, source=self.key)
        return FetchedArticle(item=item, body_text=body)
