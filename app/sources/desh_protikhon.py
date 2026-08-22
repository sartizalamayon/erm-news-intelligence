"""Desh Protikhon — Bengali general daily. #41 by article volume.
WordPress REST API is open. Config ported from
`backend/collector_logic.py` SOURCES list (:358-359).

Falls back to Google News search (`app.sources._fallback`) when the
wp_rest call fails. Measured live 2026-08-18: a live run recorded
"could not establish a connection within 5s" (a connect timeout), but a
manual retry moments later succeeded in 3s — an intermittently flaky
host, not a dead one, the same shape as Nagorik earlier this session.
See `_fallback.py`'s docstring for why this is a fallback rather than a
permanent method swap.
"""

from __future__ import annotations

from app.sources._body import fetch_generic_article_body
from app.sources._fallback import fetch_with_gnews_fallback
from app.sources._wp_rest import fetch_wp_rest
from app.sources.base import FeedItem, FetchedArticle, NewsSource
from app.sources.http import make_session
from app.sources.registry import register

SITE_URL = "https://deshprotikhon.com"
DOMAIN = "deshprotikhon.com"


@register
class DeshProtikhonSource(NewsSource):
    key = "desh_protikhon"
    display_name = "Desh Protikhon"

    def __init__(self) -> None:
        self.session = make_session(pool_size=8)

    def iter_feed_items(self) -> list[FeedItem]:
        return fetch_with_gnews_fallback(
            lambda: fetch_wp_rest(self.session, SITE_URL, source=self.key), self.session, DOMAIN, source=self.key
        )

    def fetch_article(self, item: FeedItem) -> FetchedArticle:
        body = fetch_generic_article_body(self.session, item.url, source=self.key)
        return FetchedArticle(item=item, body_text=body)
