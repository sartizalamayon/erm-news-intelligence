"""BD Pratidin — Bengali general daily. #6 by article volume. Its own
RSS feed is CAPTCHA-protected (bot-blocked); Google News search is the
sole authoritative method for this source specifically (not a top-up —
speed-techniques.md §4 point 4). Config ported from
`backend/collector_logic.py` SOURCES list (:174-176).
"""

from __future__ import annotations

from app.sources._body import fetch_generic_article_body
from app.sources._gnews import fetch_gnews
from app.sources.base import FeedItem, FetchedArticle, NewsSource
from app.sources.http import make_session
from app.sources.registry import register

DOMAIN = "bd-pratidin.com"


@register
class BdPratidinSource(NewsSource):
    key = "bd_pratidin"
    display_name = "BD Pratidin"

    def __init__(self) -> None:
        self.session = make_session(pool_size=4)

    def iter_feed_items(self) -> list[FeedItem]:
        return fetch_gnews(self.session, DOMAIN, source=self.key)

    def fetch_article(self, item: FeedItem) -> FetchedArticle:
        body = fetch_generic_article_body(self.session, item.url, source=self.key)
        return FetchedArticle(item=item, body_text=body)
