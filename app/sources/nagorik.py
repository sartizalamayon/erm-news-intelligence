"""Nagorik — Bengali general daily. #15 by article volume. WordPress
REST API is open. Config ported from `backend/collector_logic.py`
SOURCES list (:289-291).

Falls back to Google News search (`app.sources._fallback`) when the
wp_rest call fails — measured live 2026-08-18: the whole site (not just
`/wp-json/`) was unreachable even at a 40s timeout, a real outage, not a
slow-but-alive host. See `_fallback.py`'s docstring for why this is a
fallback rather than a permanent method swap like Daily Sun/Kalbela.
"""

from __future__ import annotations

from app.sources._body import fetch_generic_article_body
from app.sources._fallback import fetch_with_gnews_fallback
from app.sources._wp_rest import fetch_wp_rest
from app.sources.base import FeedItem, FetchedArticle, NewsSource
from app.sources.http import make_session
from app.sources.registry import register

SITE_URL = "https://nagorik.com"
DOMAIN = "nagorik.com"


@register
class NagorikSource(NewsSource):
    key = "nagorik"
    display_name = "Nagorik"

    def __init__(self) -> None:
        self.session = make_session(pool_size=8)

    def iter_feed_items(self) -> list[FeedItem]:
        return fetch_with_gnews_fallback(
            lambda: fetch_wp_rest(self.session, SITE_URL, source=self.key), self.session, DOMAIN, source=self.key
        )

    def fetch_article(self, item: FeedItem) -> FetchedArticle:
        body = fetch_generic_article_body(self.session, item.url, source=self.key)
        return FetchedArticle(item=item, body_text=body)
