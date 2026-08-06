"""NDTV — English Indian news. #16 by article volume. Cloudflare blocks
the main domain's sitemap/RSS, but FeedBurner section mirrors are open;
4 feeds merged give broad (recent-window-only) coverage. Config ported
from `backend/collector_logic.py` SOURCES list (:243-250).
"""

from __future__ import annotations

from app.sources._body import fetch_generic_article_body
from app.sources._rss import fetch_rss_multi
from app.sources.base import FeedItem, FetchedArticle, NewsSource
from app.sources.http import make_session
from app.sources.registry import register

FEED_URLS = [
    "https://feeds.feedburner.com/ndtvnews-latest",
    "https://feeds.feedburner.com/ndtvnews-top-stories",
    "https://feeds.feedburner.com/ndtvnews-india-news",
    "https://feeds.feedburner.com/ndtvnews-world-news",
]


@register
class NdtvSource(NewsSource):
    key = "ndtv"
    display_name = "NDTV"

    def __init__(self) -> None:
        self.session = make_session(pool_size=8)

    def iter_feed_items(self) -> list[FeedItem]:
        return fetch_rss_multi(self.session, FEED_URLS, source=self.key)

    def fetch_article(self, item: FeedItem) -> FetchedArticle:
        body = fetch_generic_article_body(self.session, item.url, source=self.key)
        return FetchedArticle(item=item, body_text=body)
