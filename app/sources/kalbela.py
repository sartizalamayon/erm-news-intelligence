"""Kalbela — Bengali general daily. #4 by article volume. Sits behind a
Cloudflare edge that 403s Python's `requests` but serves system `curl`
normally (TLS 1.3 handshake mismatch, not bot-detection) — fetched via
`app.sources._curl_sitemap`. Config ported from
`backend/collector_logic.py` SOURCES list (:396-397).
"""

from __future__ import annotations

from app.sources._body import fetch_generic_article_body
from app.sources._curl_sitemap import fetch_curl_sitemap
from app.sources.base import FeedItem, FetchedArticle, NewsSource
from app.sources.http import make_session
from app.sources.registry import register

SITEMAP_URL = "https://www.kalbela.com/sitemap/news-sitemap.xml"


@register
class KalbelaSource(NewsSource):
    key = "kalbela"
    display_name = "Kalbela"

    def __init__(self) -> None:
        # No requests.Session for feed discovery (curl handles that
        # transport) — kept only for fetch_article()'s generic body
        # extraction, which most Kalbela article pages tolerate fine
        # over plain requests even though the sitemap XML itself doesn't.
        self.session = make_session(pool_size=8)

    def iter_feed_items(self) -> list[FeedItem]:
        return fetch_curl_sitemap(SITEMAP_URL, source=self.key)

    def fetch_article(self, item: FeedItem) -> FetchedArticle:
        body = fetch_generic_article_body(self.session, item.url, source=self.key)
        return FetchedArticle(item=item, body_text=body)
